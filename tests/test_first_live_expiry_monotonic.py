"""Synthetic clocks and intercepted claim boundary; no store, Key or network."""
import json
import unittest
from contextlib import ExitStack
from types import SimpleNamespace as Box
from unittest.mock import patch

from researchops_completion_timing import first_live_start as start

STAMP = '2026-09-06T01:00:00Z'
EXPIRY = '2026-09-06T01:01:00Z'
SECOND = 1_000_000_000


class FirstLiveExpiryTests(unittest.TestCase):
    def setUp(self):
        self.ns = 0
        self.stamp = STAMP
        self.stack = ExitStack(); self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(start, '_monotonic_ns', side_effect=lambda: self.ns))
        self.stack.enter_context(patch.object(start, '_utc_now', side_effect=lambda: self.stamp))

    def test_stalled_utc_cannot_move_previously_bound_expiry(self):
        budget = start._StartBudget()
        self.assertEqual(budget.checkpoint(EXPIRY)[1], 30 * SECOND)
        self.ns = 70 * SECOND
        with self.assertRaisesRegex(ValueError, 'authorization_expired'):
            budget.checkpoint(EXPIRY)

    def test_first_expiry_binding_includes_earlier_verification_time(self):
        budget = start._StartBudget()
        budget.checkpoint()
        self.ns = 70 * SECOND
        with self.assertRaisesRegex(ValueError, 'authorization_expired'):
            budget.checkpoint(EXPIRY)

    def test_input_read_time_before_first_checkpoint_is_not_discarded(self):
        budget = start._StartBudget()
        self.ns = 70 * SECOND
        with self.assertRaisesRegex(ValueError, 'authorization_expired'):
            budget.checkpoint(EXPIRY)

    def test_bound_expiry_survives_omitted_or_later_expiry(self):
        for expiry in (None, '2026-09-06T01:05:00Z'):
            self.ns = 0
            budget = start._StartBudget(); budget.checkpoint(EXPIRY)
            self.ns = 70 * SECOND
            with self.subTest(expiry=expiry), self.assertRaisesRegex(ValueError, 'authorization_expired'):
                budget.checkpoint(expiry, reserve_ns=0)

    def test_forward_utc_jump_tightens_and_stall_does_not_relax(self):
        budget = start._StartBudget(); budget.checkpoint(EXPIRY, reserve_ns=0)
        self.ns = 5 * SECOND; self.stamp = '2026-09-06T01:00:40Z'
        self.assertEqual(budget.checkpoint(EXPIRY, reserve_ns=0)[1], 25 * SECOND)
        self.ns = 15 * SECOND
        self.assertEqual(budget.checkpoint(EXPIRY, reserve_ns=0)[1], 25 * SECOND)
        self.ns = 25 * SECOND
        with self.assertRaisesRegex(ValueError, 'authorization_expired'):
            budget.checkpoint(EXPIRY, reserve_ns=0)

    def test_sealing_reserve_and_whole_budget_remain_independent_limits(self):
        budget = start._StartBudget(); self.ns = 30 * SECOND
        with self.assertRaisesRegex(ValueError, 'budget_exhausted'):
            budget.checkpoint(EXPIRY)
        self.assertEqual(budget.checkpoint(EXPIRY, reserve_ns=0)[1], 60 * SECOND)
        self.ns = 0; budget = start._StartBudget()
        self.ns = 330 * SECOND
        with self.assertRaisesRegex(ValueError, 'budget_exhausted'):
            budget.checkpoint('2026-09-06T02:00:00Z', reserve_ns=0)

    def test_expired_preparation_never_reaches_claim_for_either_version(self):
        proof = Box(execution_environment_id='fixture-environment', execution_commit='c' * 40,
                    expires_at_utc=EXPIRY)
        plan = json.dumps({'binding': {'execution_tree': 'd' * 40,
                            'source_integrity_commitment_sha256': 'e' * 64}}).encode()
        def slow_source(*args, **kwargs):
            self.ns = 70 * SECOND
            return Box()
        with ExitStack() as stack:
            stack.enter_context(patch.object(start, '_source_functions', return_value=(
                {'first_live': ('unused', 'unused')}, lambda root: None, slow_source, lambda *a, **k: None)))
            stack.enter_context(patch.object(start.control, 'verify_first_live_authorization', return_value=proof))
            stack.enter_context(patch.object(start.local_claim, 'local_claim_store_status', return_value={
                'status': 'ready', 'execution_environment_id': proof.execution_environment_id}))
            stack.enter_context(patch.object(start, '_current_file', return_value=json.dumps({
                'implementation_commitment_sha256': 'f' * 64}).encode()))
            intent = stack.enter_context(patch.object(start.control, 'build_first_live_claim_intent',
                side_effect=AssertionError('expiry must precede claim intent')))
            claim = stack.enter_context(patch.object(start.local_claim, '_reserve_with_ownership',
                side_effect=AssertionError('no real claim allowed')))
            for version in (4, 5):
                self.ns = 0
                with self.subTest(version=version), self.assertRaisesRegex(start.FirstLiveStartError, 'authorization_expired') as caught:
                    start._prepare_claimed_first_live_start_impl(None, plan_bytes=plan,
                        authorization_bytes=b'fixture-auth', expected_authorization_binding_sha256='a' * 64,
                        _budget=None, _implementation_version=version)
                self.assertFalse(caught.exception.claim_consumed)
            intent.assert_not_called(); claim.assert_not_called()


if __name__ == '__main__': unittest.main()
