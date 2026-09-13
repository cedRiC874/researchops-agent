"""Budget comparisons are exact and do not inherit the caller's Decimal state."""

from __future__ import annotations

import copy
import unittest
from decimal import (
    Context, DivisionByZero, FloatOperation, Inexact, InvalidOperation,
    Overflow, ROUND_DOWN, ROUND_HALF_EVEN, Rounded, Subnormal, Underflow,
    localcontext,
)
from pathlib import Path
from unittest.mock import patch

from researchops_external_closure.documents import _verify_envelope_commitments,_domain_hash,_RUNTIME_PLAN_DOMAIN
from researchops_external_closure.errors import ExternalClosurePrimitiveError
from researchops_external_closure.pre_receipt import verify_preregistration_envelope
from researchops_external_closure.primitives import canonical_json_bytes
from tests.test_external_closure_documents import SyntheticPreReceipt


ROOT=Path(__file__).resolve().parents[1]


def contexts():
    return (
        Context(prec=28,rounding=ROUND_HALF_EVEN),
        Context(prec=1,rounding=ROUND_HALF_EVEN),
        Context(prec=1,rounding=ROUND_DOWN,Emin=-1,Emax=1,
                traps=[DivisionByZero,FloatOperation,Inexact,InvalidOperation,Overflow,Rounded,Subnormal,Underflow]),
        Context(prec=80,rounding=ROUND_DOWN),
    )


def context_state(context):
    return (context.prec,context.rounding,context.Emin,context.Emax,context.capitals,context.clamp,
            dict(context.flags),dict(context.traps))


class BudgetContextTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixture=SyntheticPreReceipt()
        cls.documents,cls.observation=cls.fixture.inputs()

    def envelope(self,price):
        envelope=copy.deepcopy(self.fixture.documents["preregistration_envelope"])
        runtime=envelope["runtime_plan"]
        budget=runtime["budget_policy"]
        budget.update(input_token_limit_total=1,output_token_limit_total=1,
            input_price_per_million_cny=price,output_price_per_million_cny=price,
            local_observed_cost_stop_cny="0.000001")
        body=dict(budget);body.pop("budget_policy_commitment_sha256")
        budget["budget_policy_commitment_sha256"]=_domain_hash(_RUNTIME_PLAN_DOMAIN,b"budget_policy",canonical_json_bytes(body))
        body=dict(runtime);body.pop("external_plan_binding_sha256")
        runtime["external_plan_binding_sha256"]=_domain_hash(_RUNTIME_PLAN_DOMAIN,canonical_json_bytes(body))
        return envelope

    def test_tiny_over_budget_is_rejected_under_every_decimal_context(self):
        envelope=self.envelope("0.500001")
        for source in contexts():
            with self.subTest(precision=source.prec,rounding=source.rounding,Emax=source.Emax),localcontext(source) as context:
                before=context_state(context)
                with self.assertRaisesRegex(ExternalClosurePrimitiveError,"external_closure_budget_invalid"):
                    _verify_envelope_commitments(envelope,self.fixture.documents["seen_case_exclusion_manifest"])
                self.assertEqual(context_state(context),before)

    def test_at_or_below_budget_is_accepted_without_rounding_or_context_flags(self):
        for price in ("0.500000","0.499999"):
            envelope=self.envelope(price)
            for source in contexts():
                with self.subTest(price=price,precision=source.prec,Emax=source.Emax),localcontext(source) as context:
                    before=context_state(context)
                    _verify_envelope_commitments(envelope,self.fixture.documents["seen_case_exclusion_manifest"])
                    self.assertEqual(context_state(context),before)

    def test_public_signed_pre_receipt_result_is_context_independent(self):
        expected=None
        for source in contexts():
            with self.subTest(precision=source.prec,Emax=source.Emax),localcontext(source) as context,patch(
                "socket.socket",side_effect=AssertionError("network forbidden")
            ):
                before=context_state(context)
                result=verify_preregistration_envelope(ROOT,self.documents,external_observation_bundle=self.observation)
                if expected is None:
                    expected=result
                self.assertEqual(result,expected)
                self.assertEqual(context_state(context),before)


if __name__=="__main__":
    unittest.main()
