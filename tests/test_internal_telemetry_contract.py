"""Internal fixed policy and synthetic tasks; no real Key/store/network."""
import copy
import unittest

from researchops_internal_telemetry import contract as c
from researchops_completion_timing.campaign_budget import _CampaignBudget, CampaignBudgetError


def fake_pricing():
    # Synthetic offline fixture, never current official pricing evidence.
    return dict(schema_version="provider-completion-internal-pricing/1.0",status="user_reviewed_official_snapshot",
        provider_id="deepseek",requested_model="deepseek-v4-flash",input_price_per_million_cny="2.000000",
        output_price_per_million_cny="8.000000",cache_discount_assumed=False,provider_invoice_hard_cap=False,
        evidence_date_utc=c.now().isoformat().replace("+00:00","Z"),official_evidence_sha256="1"*64)


class InternalContractTests(unittest.TestCase):
    def setUp(self):
        self.policy=c.load_policy()
        self.tasks=c.decode(c.read(c.ROOT/c.DIRECTORY/"cases_v1.json"))

    def test_fixed_30_known_cases_and_order(self):
        c.validate_tasks(self.tasks,self.policy)
        self.assertEqual(len(self.tasks["cases"]),30)
        self.assertEqual([len(x["input"].encode()) for x in self.tasks["cases"][15:25]],
            [64,128,256,512,1024,4092,4093,4094,4095,4096])

    def test_case_mutations_reject(self):
        for key,value in (("case_index",True),("expected_state","incomplete_length"),("max_output_tokens",16),
                          ("input","x"*4097),("case_id","P6-DEV-001")):
            with self.subTest(key=key):
                changed=copy.deepcopy(self.tasks);changed["cases"][0][key]=value
                with self.assertRaises(c.InternalError):c.validate_tasks(changed,self.policy)
        changed=copy.deepcopy(self.tasks);changed["cases"][1]["input"]=changed["cases"][0]["input"]
        changed["cases"][1]["input_sha256"]=changed["cases"][0]["input_sha256"]
        with self.assertRaisesRegex(c.InternalError,"duplicate"):c.validate_tasks(changed,self.policy)

    def test_pending_price_does_not_authorize_budget(self):
        pending=c.decode(c.read(c.ROOT/c.DIRECTORY/"pricing.pending.json"))
        with self.assertRaisesRegex(c.InternalError,"pricing_not_ready"):c.validate_pricing(pending)

    def test_budget_exact_input_boundary_complete_30(self):
        budget=_CampaignBudget(c.budget_plan(self.policy,self.tasks,fake_pricing()))
        for i in range(30):
            budget.begin_case(i);reservation=budget.reserve_request()
            self.assertEqual(reservation.output_token_cap,512)
            budget.observe_usage(reservation,input_tokens=2048,output_tokens=512)
            budget.finish_case()
        self.assertEqual(budget.summary()["input_tokens"],61440)
        self.assertEqual(budget.summary()["completed_case_count"],30)

    def test_budget_overshoot_retains_actual_usage(self):
        budget=_CampaignBudget(c.budget_plan(self.policy,self.tasks,fake_pricing()))
        budget.begin_case(0);reservation=budget.reserve_request()
        with self.assertRaisesRegex(CampaignBudgetError,"observed_limit_exceeded"):
            budget.observe_usage(reservation,input_tokens=61441,output_tokens=1)
        self.assertEqual(budget.summary()["known_input_tokens"],61441)
        with self.assertRaises(CampaignBudgetError):budget.reserve_request()

    def test_missing_usage_is_unknown_not_zero(self):
        budget=_CampaignBudget(c.budget_plan(self.policy,self.tasks,fake_pricing()))
        budget.begin_case(0);reservation=budget.reserve_request()
        with self.assertRaisesRegex(CampaignBudgetError,"usage_unknown"):
            budget.observe_usage(reservation,input_tokens=None,output_tokens=1)
        self.assertIsNone(budget.summary()["input_tokens"])

    def test_privacy_blocks_known_patterns_before_input_admission(self):
        for text in ("sk-FAKESECRET0123456789","Authorization: Bearer fake","C:/private/synthetic.txt",
                     "Traceback (most recent call last):", "fake@example.invalid"):
            with self.subTest(pattern=text.split()[0]):
                changed=copy.deepcopy(self.tasks);changed["cases"][0]["input"]=text
                changed["cases"][0]["input_sha256"]=c.digest(text.encode())
                with self.assertRaisesRegex(Exception,"sensitive_content_detected"):c.validate_tasks(changed,self.policy)

    def test_no_tools_or_retry_or_external_closure(self):
        self.assertEqual((self.policy["tools"],self.policy["handoffs"],self.policy["sdk_retries"],self.policy["http_retries"]),(0,0,0,0))
        self.assertFalse(self.policy["status_closure_allowed"])

    def test_provider_error_code_cannot_echo_a_secret(self):
        class ProviderError(Exception):
            __module__="openai"
            code="synthetic_secret_which_matches_identifier_regex"
        self.assertEqual(c.safe_error_code(ProviderError()),"internal_execution_failed")

    def test_output_cap_overrun_and_duplicate_reservation_stop(self):
        budget=_CampaignBudget(c.budget_plan(self.policy,self.tasks,fake_pricing()))
        budget.begin_case(0);reservation=budget.reserve_request()
        with self.assertRaisesRegex(CampaignBudgetError,"observed_limit_exceeded"):
            budget.observe_usage(reservation,input_tokens=5,output_tokens=513)
        self.assertEqual(budget.summary()["known_output_tokens"],513)
        with self.assertRaises(CampaignBudgetError):budget.reserve_request()
        fresh=_CampaignBudget(c.budget_plan(self.policy,self.tasks,fake_pricing()))
        fresh.begin_case(0);fresh.reserve_request()
        with self.assertRaises(CampaignBudgetError):fresh.reserve_request()

    def test_json_escaping_does_not_hide_real_unc_or_encoded_secret(self):
        c.decode(c.raw({"input": "marker\\segment"}))
        for text in ("\\\\server\\share", "C:\\sensitive\\file", "Authorization: Bearer synthetic"):
            with self.assertRaises(Exception):c.decode(c.raw({"input":text}))
        with self.assertRaises(Exception):c.decode(b'{"input":"\\u0073k-FAKESECRET0123456789"}')


if __name__=="__main__":unittest.main()
