import copy
import unittest
from researchops_item6_experiment_v1 import contract as c
from researchops_item6_experiment_v1.budget import Budget
from . import item6_experiment_fixture as f


class BudgetTests(unittest.TestCase):
    def test_observed_usage_is_not_equivalent_to_settled_cost(self):
        from decimal import Decimal
        valid = dict(usage=dict(complete=True, normalized=dict(requests=1,input_tokens=100,output_tokens=30,total_tokens=130,cached_input_tokens=0,reasoning_tokens=0)))
        for key,value,code in (("cached_input_tokens",101,"usage_detail"),("reasoning_tokens",31,"usage_detail"),
                               ("requests",2,"usage_inconsistent"),("total_tokens",131,"usage_inconsistent"),
                               ("input_tokens",True,"usage_unknown"),("input_tokens",-1,"usage_unknown")):
            with self.subTest(key=key):
                budget=Budget(self.fixture()); budget.reserve("IC-01"); budget.settle(copy.deepcopy(valid))
                known=budget.snapshot()["known_cost_subtotal"]
                bad=copy.deepcopy(valid);bad["usage"]["normalized"][key]=value
                budget.reserve("IC-02")
                with self.assertRaisesRegex(c.ExperimentError,code): budget.settle(bad)
                result=budget.snapshot()
                self.assertIsNone(result["cost_total"])
                self.assertEqual(result["known_cost_subtotal"],known)
                self.assertGreater(Decimal(known),0)
                self.assertFalse(result["requests"][-1]["cost_settled"])
                self.assertEqual(result["requests"][-1]["usage"],bad["usage"]["normalized"])
        self.assertEqual(Budget(self.fixture()).snapshot()["cost_total"],"0")

    def test_valid_observed_overshoot_remains_settled(self):
        budget=Budget(self.fixture()); budget.reserve("IC-01")
        count=budget.limits["output_per_request"]+1
        record=dict(usage=dict(complete=True,normalized=dict(requests=1,input_tokens=100,output_tokens=count,total_tokens=count+100)))
        with self.assertRaisesRegex(c.ExperimentError,"output_overshoot"): budget.settle(record)
        self.assertTrue(budget.snapshot()["requests"][0]["cost_settled"])
        self.assertIsNotNone(budget.snapshot()["cost_total"])

    def test_cache_overrun_actual_adapter_cost_is_unknown(self):
        result=f.run_case("cache_overrun")
        self.assertEqual(result["process_exit_code"],2)
        self.assertIn("cache_overrun",result["hits"])
        budget=result["artifact"]["budget"]
        self.assertEqual(len(result["calls"]),1)
        self.assertIsNone(budget["cost_total"])
        self.assertEqual(budget["known_cost_subtotal"],"0")
        self.assertEqual(budget["requests"][0]["usage"]["input_tokens"],100)
        self.assertEqual(budget["requests"][0]["usage"]["output_tokens"],30)
        self.assertFalse(budget["requests"][0]["cost_settled"])

    def fixture(self):
        return {"budget": c.policy()["limits"], "mode": "offline_test", "pricing": dict(kind="synthetic_fixture", input_per_million="1", output_per_million="2", cost_limit="10")}

    def test_request_and_tool_denominators_are_separate(self):
        budget = Budget(self.fixture())
        budget.tool("IC-01:fixed_workflow")
        self.assertEqual(budget.requests, [])
        budget.reserve("IC-01")
        with self.assertRaisesRegex(c.ExperimentError, "request_overlap"): budget.reserve("IC-02")
        budget.wire(100); budget.dispatch()
        with self.assertRaisesRegex(c.ExperimentError, "repeat_dispatch"): budget.dispatch()

    def test_unknown_usage_not_zero_cost(self):
        budget = Budget(self.fixture()); budget.reserve("IC-01"); budget.unknown("no_response")
        self.assertIsNone(budget.snapshot()["cost_total"])
        self.assertEqual(budget.snapshot()["known_cost_subtotal"], "0")

    def test_real_adapter_usage_faults_preserve_full_plan(self):
        for mode in ("missing_usage", "usage_boolean", "usage_overrun", "cache_overrun"):
            with self.subTest(mode=mode):
                result = f.unfinished_case(mode) if mode == "missing_usage" else f.run_case(mode)
                self.assertEqual(result["process_exit_code"], 2)
                self.assertEqual(len(result["calls"]), 1)
                self.assertIn(mode, result["hits"])
                self.assertIsNotNone(result["run"], result)
                for path in ("agent", "fixed_workflow"):
                    self.assertEqual(len(result["artifact"]["business"][path]), 16)
                if mode == "missing_usage":
                    self.assertEqual(result["run"]["archive_kind"], "unfinished_failure")
                    self.assertFalse(result["artifact"]["phase"]["closed"])
                    self.assertIsNone(result["artifact"]["phase"]["phase"]["phase_terminal_ns"])
                    self.assertIsNotNone(result["artifact"]["phase"]["active_attempt"])
                    self.assertIsNone(result["artifact"]["budget"]["cost_total"])
                    self.assertEqual(len(result["attempt_failures"]), 1)
                    self.assertEqual(result["attempt_failures"][0]["exception_type"], "CompletionTelemetryError")
                    self.assertEqual(result["attempt_failures"][0]["code"], "completion_telemetry_comparable_counter_invalid")
                    checked = f.verify_unfinished(result)
                    self.assertTrue(checked["failure_archive_verified"])
                    self.assertFalse(checked["phase_completed"])

    def test_business_read_failure_is_not_an_authority_stop(self):
        result = f.run_case("tool_failure")
        self.assertEqual(result["process_exit_code"], 0, result.get("error"))
        self.assertEqual(len(result["calls"]), 30)
        row = result["artifact"]["business"]["agent"]["IC-01"]
        self.assertEqual(row["status"], "failed")
        self.assertEqual(row["known_failures"][0]["attribution"], "source_or_tool_not_model_verdict")
        self.assertEqual(result["artifact"]["scores"]["agent"]["raw_report"]["tasks"][0]["task_verdict"], "fail")

    def test_tool_timeout_stops_new_dispatch_and_keeps_missing_terminal_observation(self):
        result = f.run_case("tool_timeout")
        self.assertEqual(result["process_exit_code"], 2, result.get("error"))
        self.assertIn("tool_timeout", result["hits"])
        self.assertEqual(len(result["calls"]), 1)
        self.assertEqual(result["network_attempts"], 0)
        artifact = result["artifact"]
        self.assertEqual(artifact["status"], "failed")
        self.assertEqual(artifact["authority"]["stopped"], "item6_tool_timeout")
        row = artifact["business"]["agent"]["IC-01"]
        self.assertEqual(row["execution_state"], "observed")
        self.assertEqual(row["completion"], "timeout")
        self.assertEqual(row["status"], "failed")
        self.assertFalse(row["events_complete"])
        self.assertEqual(row["allowed_evidence"], [])
        self.assertTrue(all(event["produced_artifacts"] == [] for event in row["events"]))
        self.assertTrue(any(failure["code"] == "item6_tool_timeout" for failure in row["known_failures"]))
        self.assertEqual(row["events"][0]["result"], {"error": "item6_tool_timeout"})
        for path in ("agent", "fixed_workflow"):
            self.assertEqual(len(artifact["business"][path]), 16)
            self.assertEqual(artifact["scores"][path]["raw_report"]["counts"]["planned"], 16)
        task = artifact["scores"]["agent"]["raw_report"]["tasks"][0]
        self.assertEqual(task["task_verdict"], "fail")
        self.assertEqual(task["dimensions"]["delivery"]["status"], "fail")
        checks = task["dimensions"]["delivery"]["checks"]
        self.assertTrue(any(check["code"] == "timeout" and check["status"] == "fail" for check in checks))
        self.assertTrue(any(check["code"] == "final_output_unobserved" and check["status"] == "unknown" for check in checks))
        events = artifact["audit"]["events"]
        related = [event for event in events if event["safe_payload"].get("case_run_id") == row["run_id"]]
        self.assertTrue(any(event["event_type"] == "item6_tool_started_v1" for event in related))
        self.assertFalse(any(event["event_type"] == "item6_tool_finished_v1" for event in related))
        f.RESULTS.append(dict(kind="tool_timeout_boundary", actual_exit_code=2, dispatches=1,
            fixed_denominator=16, agent_denominator=16, missing_tool_terminal_preserved=True,
            delivery_fail_code="timeout", delivery_unknown_code="final_output_unobserved", task_verdict="fail"))

    def test_missing_facts_return_does_not_publish_a_success_artifact(self):
        result = f.run_case("tool_missing_facts")
        self.assertEqual(result["process_exit_code"], 2)
        self.assertIn("tool_missing_facts", result["hits"])
        self.assertIn("declared_stop_after_boundary_case", result["hits"])
        self.assertEqual(len(result["calls"]), 2)
        artifact = result["artifact"]
        self.assertEqual(artifact["authority"]["stopped"], "item6_fixture_boundary_case_stop")
        row = artifact["business"]["agent"]["IC-01"]
        self.assertEqual(row["events"][0]["status"], "failed")
        self.assertEqual(row["events"][0]["produced_artifacts"], [])
        self.assertEqual(row["allowed_evidence"], [])
        self.assertTrue(row["events_complete"])
        self.assertEqual(row["known_failures"][0]["attribution"], "source_or_tool_not_model_verdict")
        for path in ("agent", "fixed_workflow"):
            self.assertEqual(artifact["scores"][path]["raw_report"]["counts"]["planned"], 16)
        self.assertEqual(artifact["scores"]["agent"]["raw_report"]["tasks"][0]["task_verdict"], "fail")
        f.RESULTS.append(dict(kind="missing_facts_boundary", injection_hit=True, dispatches=2,
            batch_stop="declared_test_boundary_after_IC01", business_failure="source_or_tool_not_model_verdict",
            complete_tool_failure_observation=True, allowed_evidence_count=0, formal_comparison_result=False))
