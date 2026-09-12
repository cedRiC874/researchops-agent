"""Accounting policy/unknowns/concurrency, not network or authorization evidence."""
import copy
from decimal import localcontext, Inexact, Rounded
import threading
import unittest
from researchops_completion_timing.campaign_budget import _CampaignBudget, CampaignBudgetError, _budget_for_opened_owner
from tests.test_external_closure_documents import SyntheticPreReceipt


class CampaignBudgetTests(unittest.TestCase):
    def plan(self):
        return copy.deepcopy(SyntheticPreReceipt().documents['preregistration_envelope']['runtime_plan'])

    def test_reserved_requests_are_not_claimed_as_network_calls(self):
        budget = _CampaignBudget(self.plan()); budget.begin_case(0); token = budget.reserve_request()
        state = budget.summary()
        self.assertEqual(state['reserved_request_count'], 1)
        self.assertIsNone(state['input_tokens']); self.assertIsNone(state['observed_cost_cny'])
        self.assertFalse(state['provider_calls_independently_verified'])
        self.assertFalse(state['runtime_authority_granted'])
        budget.observe_usage(token, input_tokens=10, output_tokens=2)
        budget.finish_case()
        self.assertEqual(budget.summary()['observed_cost_cny'], '0.000048000000')

    def test_remaining_output_must_fit_the_frozen_fixed_request_cap(self):
        plan = self.plan(); plan['budget_policy']['output_token_limit_total'] = 600
        budget = _CampaignBudget(plan); budget.begin_case(0)
        first = budget.reserve_request(); self.assertEqual(first.output_token_cap, 512)
        budget.observe_usage(first, input_tokens=1, output_tokens=500); budget.finish_case(); budget.begin_case(1)
        with self.assertRaisesRegex(CampaignBudgetError, 'exhausted'): budget.reserve_request()
        self.assertEqual(budget.summary()['completed_case_count'], 1)
        self.assertEqual(budget.summary()['reserved_request_count'], 1)

    def test_unknown_usage_preserves_known_lower_bounds_and_stops(self):
        budget = _CampaignBudget(self.plan()); budget.begin_case(0)
        first = budget.reserve_request(); budget.observe_usage(first, input_tokens=10, output_tokens=5)
        second = budget.reserve_request(); budget.unknown_outcome(second)
        state = budget.summary()
        self.assertEqual(state['known_input_tokens'], 10); self.assertEqual(state['known_output_tokens'], 5)
        self.assertIsNone(state['input_tokens']); self.assertIsNone(state['output_tokens']); self.assertIsNone(state['provider_bill_cny'])
        with self.assertRaises(CampaignBudgetError): budget.reserve_request()

    def test_missing_or_bool_usage_never_becomes_zero(self):
        for value in (None, True, -1, 1.0):
            budget = _CampaignBudget(self.plan()); budget.begin_case(0); token = budget.reserve_request()
            with self.subTest(value=value), self.assertRaisesRegex(CampaignBudgetError, 'usage_unknown'):
                budget.observe_usage(token, input_tokens=value, output_tokens=0)
            self.assertIsNone(budget.summary()['input_tokens'])

    def test_overshoot_is_retained_instead_of_clamped(self):
        plan = self.plan(); plan['budget_policy']['input_token_limit_total'] = 10
        budget = _CampaignBudget(plan); budget.begin_case(0); token = budget.reserve_request()
        with self.assertRaisesRegex(CampaignBudgetError, 'observed_limit_exceeded'):
            budget.observe_usage(token, input_tokens=11, output_tokens=1)
        self.assertEqual(budget.summary()['input_tokens'], 11)
        self.assertTrue(budget.summary()['halted'])

    def test_provider_output_beyond_requested_cap_is_retained_and_rejected(self):
        budget = _CampaignBudget(self.plan()); budget.begin_case(0); token = budget.reserve_request()
        with self.assertRaisesRegex(CampaignBudgetError, 'observed_limit_exceeded'):
            budget.observe_usage(token, input_tokens=1, output_tokens=token.output_token_cap + 1)
        self.assertEqual(budget.summary()['known_output_tokens'], 513)

    def test_exact_limit_allows_case_finish_but_not_another_send(self):
        plan = self.plan(); plan['budget_policy']['input_token_limit_total'] = 10
        budget = _CampaignBudget(plan); budget.begin_case(0); token = budget.reserve_request()
        budget.observe_usage(token, input_tokens=10, output_tokens=1); budget.finish_case(); budget.begin_case(1)
        with self.assertRaisesRegex(CampaignBudgetError, 'exhausted'): budget.reserve_request()
        self.assertEqual(budget.summary()['reserved_request_count'], 1)

    def test_per_case_and_global_request_caps_are_both_enforced(self):
        budget = _CampaignBudget(self.plan()); budget.begin_case(0)
        for _ in range(2):
            token = budget.reserve_request(); budget.observe_usage(token, input_tokens=1, output_tokens=1)
        with self.assertRaisesRegex(CampaignBudgetError, 'exhausted'): budget.reserve_request()
        plan = self.plan(); plan['denominator_plan']['total_model_request_cap'] = 2
        plan['transport_limits']['network_attempt_cap'] = 2
        plan['budget_policy']['output_token_limit_total'] = 1024
        budget = _CampaignBudget(plan); budget.begin_case(0)
        for _ in range(2):
            token = budget.reserve_request(); budget.observe_usage(token, input_tokens=1, output_tokens=1)
        budget.finish_case(); budget.begin_case(1)
        with self.assertRaisesRegex(CampaignBudgetError, 'exhausted'): budget.reserve_request()
        self.assertEqual(budget.summary()['reserved_request_count'], 2)

    def test_policy_values_are_copied_and_foreign_reservations_reject(self):
        plan = self.plan(); budget = _CampaignBudget(plan)
        plan['budget_policy']['output_token_limit_per_request'] = 8192
        budget.begin_case(0); token = budget.reserve_request()
        self.assertEqual(token.output_token_cap, 512)
        other = _CampaignBudget(self.plan()); other.begin_case(0); foreign = other.reserve_request()
        with self.assertRaises(CampaignBudgetError): budget.observe_usage(foreign, input_tokens=1, output_tokens=1)
        budget.observe_usage(token, input_tokens=1, output_tokens=1)
        self.assertTrue(budget.summary()['halted'])

    def test_case_order_empty_case_and_duplicate_settlement_reject(self):
        budget = _CampaignBudget(self.plan())
        with self.assertRaises(CampaignBudgetError): budget.begin_case(1)
        budget = _CampaignBudget(self.plan()); budget.begin_case(0)
        with self.assertRaises(CampaignBudgetError): budget.finish_case()
        budget = _CampaignBudget(self.plan()); budget.begin_case(0); token = budget.reserve_request()
        budget.observe_usage(token, input_tokens=1, output_tokens=1)
        with self.assertRaises(CampaignBudgetError): budget.observe_usage(token, input_tokens=1, output_tokens=1)
        self.assertEqual(budget.summary()['known_input_tokens'], 1)

    def test_concurrent_reservation_has_one_winner_and_does_not_discard_late_usage(self):
        budget = _CampaignBudget(self.plan()); budget.begin_case(0)
        tokens = []; failures = []; barrier = threading.Barrier(2)
        def reserve():
            barrier.wait()
            try: tokens.append(budget.reserve_request())
            except CampaignBudgetError: failures.append(True)
        threads = [threading.Thread(target=reserve) for _ in range(2)]
        for thread in threads: thread.start()
        for thread in threads: thread.join(timeout=5); self.assertFalse(thread.is_alive())
        self.assertEqual((len(tokens), len(failures)), (1, 1))
        budget.observe_usage(tokens[0], input_tokens=3, output_tokens=4)
        self.assertTrue(budget.summary()['halted'])
        self.assertEqual(budget.summary()['known_input_tokens'], 3)

    def test_cost_is_independent_of_ambient_decimal_precision(self):
        plan = self.plan()
        with localcontext() as context:
            context.prec = 1; context.traps[Inexact] = True; context.traps[Rounded] = True
            budget = _CampaignBudget(plan); budget.begin_case(0); token = budget.reserve_request()
            budget.observe_usage(token, input_tokens=1234, output_tokens=511)
            self.assertEqual(budget.summary()['observed_cost_cny'], '0.008301000000')

    def test_invalid_policy_and_unopened_owner_are_not_accepted(self):
        for section, key, value in (('denominator_plan', 'agents_sdk_retries', 1), ('transport_limits', 'concurrency', True),
                                    ('budget_policy', 'cache_discount_assumed', True), ('budget_policy', 'input_price_per_million_cny', 'NaN')):
            plan = self.plan(); plan[section][key] = value
            with self.subTest(key=key), self.assertRaises(CampaignBudgetError): _CampaignBudget(plan)
        with self.assertRaises(CampaignBudgetError): _budget_for_opened_owner(object())


if __name__ == '__main__': unittest.main()
