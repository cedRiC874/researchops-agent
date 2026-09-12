"""Single-flight campaign accounting; supplied usage is not Provider evidence."""
from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
import re
import threading


class CampaignBudgetError(ValueError):
    def __init__(self, code):
        self.code = code
        self.retry_authorized = False
        super().__init__(code)


def _integer(value, minimum, maximum):
    if type(value) is not int or not minimum <= value <= maximum:
        raise CampaignBudgetError('campaign_budget_integer_invalid')
    return value


def _money(value):
    if type(value) is not str or re.fullmatch(r'(?!0\.000000$)(0|[1-9][0-9]{0,6})\.[0-9]{6}', value) is None:
        raise CampaignBudgetError('campaign_budget_price_invalid')
    return Fraction(value)


def _cost_text(value):
    scaled, remainder = divmod(value.numerator * 1_000_000_000_000, value.denominator)
    if remainder: raise CampaignBudgetError('campaign_budget_cost_precision_invalid')
    return f'{scaled // 1_000_000_000_000}.{scaled % 1_000_000_000_000:012d}'


@dataclass(frozen=True, slots=True)
class _BudgetReservation:
    request_index: int
    output_token_cap: int

    def __copy__(self): raise TypeError('budget reservation cannot be copied')
    def __deepcopy__(self, memo): raise TypeError('budget reservation cannot be copied')
    def __reduce_ex__(self, protocol): raise TypeError('budget reservation cannot be serialized')


class _CampaignBudget:
    """The consumed runner must construct this from its verified envelope only.

    This class validates/copies policy values, not signatures, actual sends or
    response provenance. No IO, clocks, Key, task text or runtime permission.
    """
    def __init__(self, runtime_plan):
        try:
            if type(runtime_plan) is not dict: raise ValueError()
            denominator, limits, policy = (runtime_plan[name] for name in ('denominator_plan', 'transport_limits', 'budget_policy'))
            if any(type(value) is not dict for value in (denominator, limits, policy)): raise ValueError()
            cases = denominator['case_ids']
            if (type(cases) is not list or not 1 <= len(cases) <= 100
                or any(type(case) is not str or re.fullmatch(r'PCECASE-[A-F0-9]{32}', case) is None for case in cases)
                or len(set(cases)) != len(cases)): raise ValueError()
            self._cases = tuple(cases)
            self._turn_cap = _integer(denominator['max_turns_per_case'], 1, 8)
            self._request_cap = _integer(denominator['total_model_request_cap'], len(cases), min(800, len(cases) * self._turn_cap))
            if (type(limits['network_attempt_cap']) is not int or limits['network_attempt_cap'] != self._request_cap
                or type(limits['concurrency']) is not int or limits['concurrency'] != 1
                or type(limits['tools']) is not int or limits['tools'] != 0
                or limits['resume'] is not False or limits['fallback'] is not False
                or type(denominator['agents_sdk_retries']) is not int or denominator['agents_sdk_retries'] != 0
                or type(denominator['http_client_retries']) is not int or denominator['http_client_retries'] != 0): raise ValueError()
            self._input_cap = _integer(policy['input_token_limit_total'], 1, 1_000_000)
            self._per_output_cap = _integer(policy['output_token_limit_per_request'], 1, 8192)
            self._output_cap = _integer(policy['output_token_limit_total'], 1, min(500_000, self._request_cap * self._per_output_cap))
            self._input_price, self._output_price, self._cost_cap = (_money(policy[name]) for name in
                ('input_price_per_million_cny', 'output_price_per_million_cny', 'local_observed_cost_stop_cny'))
            if (policy['currency'] != 'CNY' or policy['token_price_unit'] != 'per_million_tokens'
                or policy['cache_discount_assumed'] is not False or policy['provider_invoice_hard_cap'] is not False
                or policy['request_count_and_output_caps_enforced_pre_send'] is not True
                or policy['single_in_flight_request_may_overshoot_observed_limits'] is not True
                or policy['input_token_limit_enforcement'] != 'post_response_observed_stop'
                or policy['cost_stop_enforcement'] != 'post_response_observed_stop'): raise ValueError()
            worst = (self._input_cap * self._input_price + self._output_cap * self._output_price) / 1_000_000
            if worst > self._cost_cap: raise ValueError()
        except (KeyError, TypeError, ValueError):
            raise CampaignBudgetError('campaign_budget_policy_invalid') from None
        self._lock = threading.Lock()
        self._requests = self._case_requests = self._next_case = 0
        self._input = self._output = 0
        self._cost = Fraction(0)
        self._case = self._inflight = None
        self._halted = self._unknown = False

    def _reject(self, code):
        self._halted = True
        raise CampaignBudgetError(code)

    def begin_case(self, case_index):
        with self._lock:
            if (self._halted or self._case is not None or self._inflight is not None or type(case_index) is not int
                or case_index != self._next_case or not 0 <= case_index < len(self._cases)):
                self._reject('campaign_budget_case_order_invalid')
            self._case, self._case_requests = case_index, 0

    def reserve_request(self):
        with self._lock:
            if self._halted or self._case is None or self._inflight is not None:
                self._reject('campaign_budget_request_state_invalid')
            if (self._requests >= self._request_cap or self._case_requests >= self._turn_cap
                or self._input >= self._input_cap or self._output_cap - self._output < self._per_output_cap or self._cost >= self._cost_cap):
                self._reject('campaign_budget_exhausted')
            # The frozen send verifier requires the signed fixed per-request
            # cap, not a dynamically reduced final request. Stop if it cannot fit.
            reservation = _BudgetReservation(self._requests, self._per_output_cap)
            self._requests += 1; self._case_requests += 1; self._inflight = reservation
            return reservation

    def observe_usage(self, reservation, *, input_tokens, output_tokens, requests=1):
        with self._lock:
            if reservation is not self._inflight or reservation is None:
                self._reject('campaign_budget_reservation_invalid')
            try:
                incoming = _integer(input_tokens, 0, 9_007_199_254_740_991)
                outgoing = _integer(output_tokens, 0, 9_007_199_254_740_991)
                _integer(requests, 1, 1)
            except CampaignBudgetError:
                self._unknown = True; self._inflight = None
                self._reject('campaign_budget_usage_unknown')
            self._input += incoming; self._output += outgoing
            self._cost = (self._input * self._input_price + self._output * self._output_price) / 1_000_000
            self._inflight = None
            if (outgoing > reservation.output_token_cap or self._input > self._input_cap
                or self._output > self._output_cap or self._cost > self._cost_cap):
                self._reject('campaign_budget_observed_limit_exceeded')

    def unknown_outcome(self, reservation):
        with self._lock:
            if reservation is not self._inflight or reservation is None:
                self._reject('campaign_budget_reservation_invalid')
            self._unknown = self._halted = True; self._inflight = None

    def finish_case(self):
        with self._lock:
            if self._halted or self._case is None or self._inflight is not None or self._case_requests == 0:
                self._reject('campaign_budget_case_not_complete')
            self._next_case += 1; self._case = None

    def summary(self):
        with self._lock:
            complete = not self._unknown and self._inflight is None
            return dict(scope='supplied_usage_accounting_only', reserved_request_count=self._requests,
                completed_case_count=self._next_case, inflight=self._inflight is not None, halted=self._halted,
                known_input_tokens=self._input, known_output_tokens=self._output, known_cost_cny=_cost_text(self._cost),
                input_tokens=self._input if complete else None, output_tokens=self._output if complete else None,
                observed_cost_cny=_cost_text(self._cost) if complete else None, usage_complete=complete,
                provider_bill_cny=None, provider_invoice_hard_cap=False, provider_calls_independently_verified=False,
                runtime_authority_granted=False)


def _budget_for_opened_owner(owner):
    """Single factory handoff; never accept an externally supplied success flag."""
    from .campaign_tasks import _CampaignTaskOwner, _CampaignTaskOwnerV5
    if type(owner) not in (_CampaignTaskOwner, _CampaignTaskOwnerV5):
        raise CampaignBudgetError('campaign_budget_owner_required')
    try:
        opening = owner._take_for_runner()
        envelope = owner._prepared._verified_envelope()
        budget = _CampaignBudget(envelope['runtime_plan'])
        if budget._cases != tuple(case['case_handle'] for case in opening._cases):
            raise CampaignBudgetError('campaign_budget_case_binding_invalid')
        return budget
    except BaseException:
        owner.abort()
        raise
