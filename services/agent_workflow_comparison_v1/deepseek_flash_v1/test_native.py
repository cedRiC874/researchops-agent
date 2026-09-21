"""Native SDK adapter tests; no substitution of the scorer or SDK model loop."""
from . import runtime as r
from .wire import build_client, run_agent, MODEL, FAKE_KEY
from ..controlled_comparison_v1.paths import Session, fixed_path
from ..controlled_comparison_v1.budget import Budget, StopRun
from copy import deepcopy
from pathlib import Path
import json
import socket
import unittest
from unittest.mock import patch
import httpx2

FAULT_REPORTS = []
NORMAL = None
FAULTS = {
    "http401": ({"http_status": 401}, "http_status_401"),
    "http429": ({"http_status": 429}, "http_status_429"),
    "http503": ({"http_status": 503}, "http_status_503"),
    "redirect": ({"http_status": 302}, "http_status_302"),
    "timeout": ({"timeout": True}, "model_timeout"),
    "cancel": ({"cancel": True}, "cancelled"),
    "invalid_json": ({"invalid_json": True}, "invalid_response_json"),
    "duplicate_json": ({"duplicate_json": True}, "invalid_response_json"),
    "missing_usage": ({"missing_usage": True}, "usage_unavailable"),
    "null_usage": ({"null_usage": True}, "usage_unavailable"),
    "usage_boolean": ({"usage_boolean": True}, "usage_unavailable"),
    "usage_total_mismatch": ({"usage_total_mismatch": True}, "usage_total_mismatch"),
    "usage_overrun": ({"usage_overrun": True}, "reported_usage_exceeds_reservation"),
    "cache_overrun": ({"cache_overrun": True}, "usage_detail_out_of_range"),
    "invalid_usage_details": ({"invalid_usage_details": True}, "invalid_usage_details"),
    "incomplete_call": ({"incomplete_call": True}, "incomplete_tool_call"),
    "incomplete_message": ({"incomplete_message": True}, "inconsistent_message_completion"),
    "wrong_model": ({"wrong_model": True}, "response_model_mismatch"),
    "reasoning_output": ({"reasoning_output": True}, "unexpected_reasoning_not_retained"),
    "truncated": ({"truncated": True}, "response_incomplete"),
    "failed_status": ({"failed_status": True}, "response_failed"),
    "in_progress": ({"in_progress": True}, "response_status_unknown"),
    "empty_output": ({"empty_output": True}, "final_output_unobserved"),
    "privacy_text": ({"privacy_text": True}, "privacy_boundary"),
    "invalid_arguments": ({"invalid_arguments": True}, "tool_arguments_invalid"),
    "multiple_tools": ({"multiple_tools": True}, "unsupported_multi_action_response"),
    "unknown_tool": ({"unknown_tool": True}, "tool_name_or_identity_invalid"),
    "duplicate_arguments": ({"duplicate_arguments": True}, "tool_arguments_invalid"),
    "oversized_response": ({"oversized_response": True}, "response_byte_limit"),
}


class NativeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        global NORMAL
        cls.tasks = r.read_json(r.OLD / "tasks/frozen/tasks.json")["tasks"]
        cls.scripts = r.read_json(r.OLD / "fixtures/development/mock_responses.json")
        cls.contracts = r.read_json(r.OLD / "scoring/contracts.json")["contracts"]
        cls.limits = r.read_json(r.OLD / "experiment_spec.json")["limits"]
        NORMAL = r.base.LOOP.run_until_complete(r.execute_batch("NATIVE-VALIDATION"))
        cls.normal = NORMAL

    def single(self, faults=None, *, tid="IC-01", text_override=False, text=None, limits=None):
        task = next(t for t in self.tasks if t["task_id"] == tid)
        script = deepcopy(self.scripts[tid])
        if text_override: script["steps"][-1]["text"] = text
        original = deepcopy((task, script))
        budget = Budget({**self.limits, **(limits or {})})
        session = Session(task, "agent", "NATIVE-FAULT-" + tid, budget, r.source_snapshot(), faults)
        budget.begin_task(session.key)
        try:
            record = r.base.LOOP.run_until_complete(run_agent(session, script))
        except StopRun as exc:
            session.record["known_failures"].append({"code": exc.code})
            record = session.finish()
        self.assertEqual((task, script), original)
        obs, evidence = r.base.observation_for_score(record)
        report = r.base.SCORING.score_case(self.contracts[tid], obs, evidence)
        row = {"faults": faults, "record": record, "budget": budget.snapshot(), "raw_score": report,
               "purpose": "adapter_fault_validation_not_business_success_rate"}
        FAULT_REPORTS.append(row)
        return row

    def test_16_per_path_actual_fcc_and_raw_report(self):
        self.assertIsNone(self.normal["budget"]["stop_reason"])
        self.assertEqual(self.normal["identity"]["scorer_commit"], r.base.SCORER)
        self.assertEqual(Path(r.base.SCORING.__file__).resolve(), r.REPO / "src/researchops_behavior_eval_v1/__init__.py")
        for path, result in self.normal["scores"].items():
            self.assertEqual(result["raw_report"], r.base.SCORING.score_plan(result["input"]))
            self.assertEqual(result["raw_report"]["counts"], {"planned": 16, "pass": 16, "fail": 0,
                "unknown": 0, "observed": 16, "not_executed": 0, "observation_missing": 0})
            for row in result["raw_report"]["tasks"]:
                self.assertEqual(row["measurement_revision"], "internal-behavior-eval-v1.1")
                self.assertEqual(row["dimensions"]["behavior"]["checks"][0]["code"], "requirements_satisfied")

    def test_pair_input_and_zero_tool_clarification_refusal(self):
        for task in self.tasks:
            tid = task["task_id"]
            a, f = (self.normal["records"][p][tid] for p in ("agent", "fixed_workflow"))
            self.assertEqual(a["task_input"], f["task_input"])
            self.assertEqual(a["input_sha256"], f["input_sha256"])
            if int(tid[-2:]) >= 11:
                self.assertEqual(a["events"], [])
                self.assertEqual(f["events"], [])
            self.assertEqual(f["model_request_count"], 0)
            self.assertEqual(f["tokens_origin"], "not_applicable")

    def test_native_request_policy_full_history_and_tool_binding(self):
        for record in self.normal["records"]["agent"].values():
            native = record["native_wire_observation"]
            self.assertEqual(native["actual_provider_calls"], 0)
            self.assertFalse(native["shared_production_adapter_used"])
            for request in native["fixture_requests"]:
                self.assertEqual(request["model"], MODEL)
                self.assertEqual(request["max_output_tokens"], 1000)
                self.assertEqual(request["reasoning"], {"effort": "none"})
                self.assertIs(request["store"], False)
                self.assertIs(request["parallel_tool_calls"], False)
                self.assertEqual({t["name"] for t in request["tools"]}, {"inspect_sources", "read_aggregate"})
                self.assertNotIn("previous_response_id", request)
            last = native["fixture_requests"][-1]["input"]
            calls = {i["call_id"]: i for i in last if i.get("type") == "function_call"}
            outputs = {i["call_id"]: json.loads(i["output"]) for i in last if i.get("type") == "function_call_output"}
            for link in record.get("native_tool_links", []):
                event = next(e for e in record["events"] if e["call_id"] == link["call_id"])
                self.assertEqual(outputs[link["sdk_call_id"]], event["result"])
                self.assertEqual(calls[link["sdk_call_id"]]["name"], event["tool"])
                self.assertEqual(link["artifact_ids"], event["produced_artifacts"])
                self.assertEqual(link["run_id"], record["run_id"])

    def test_usage_cache_and_origin(self):
        self.assertEqual(len(self.normal["budget"]["request_attempts"]), 30)
        for rec in self.normal["records"]["agent"].values():
            self.assertIsNone(rec["actual_api_tokens"])
            self.assertIsNone(rec["actual_api_cost"])
            for response in rec["native_wire_observation"]["responses"]:
                self.assertEqual(response["usage"], {"input_tokens": 100, "output_tokens": 30,
                    "total_tokens": 130, "cached_tokens": 10, "reasoning_tokens": 0})
                self.assertEqual(response["usage_origin"], "synthetic_native_responses_fixture")

    def test_missing_optional_usage_is_unknown_not_zero(self):
        row = self.single({"missing_usage_details": True})
        self.assertIsNone(row["budget"]["stop_reason"])
        usage = row["record"]["native_wire_observation"]["responses"][0]["usage"]
        self.assertIsNone(usage["cached_tokens"])
        self.assertIsNone(usage["reasoning_tokens"])

    def test_null_empty_whitespace_not_interchanged(self):
        for text, state, stop in [(None, "unobserved", "invalid_text_part"), ("", "empty", None), (" \t", "whitespace", None)]:
            row = self.single(text_override=True, text=text)
            self.assertEqual(row["record"]["final_output"], text)
            self.assertEqual(row["record"]["text_observation"], state)
            self.assertEqual(row["budget"]["stop_reason"], stop)
            self.assertEqual(row["raw_score"]["task_verdict"], "unknown" if text is None else "fail")

    def test_free_expression_retained_and_unknown_disclosed(self):
        text = "The package suggests a small mass; please read the source."
        row = self.single(text_override=True, text=text)
        self.assertEqual(row["record"]["final_output"], text)
        dim = row["raw_score"]["dimensions"]["facts"]
        self.assertEqual(dim["status"], "unknown")
        self.assertIn("unparsed_content", [x["code"] for x in dim["checks"]])

    def test_truncation_plus_usage_failure_preserves_known_fail(self):
        row = self.single({"truncated": True, "missing_usage": True})
        self.assertEqual(row["record"]["completion"], "truncated")
        self.assertIn("response_incomplete", [x["code"] for x in row["record"]["known_failures"]])
        self.assertEqual(row["budget"]["stop_reason"], "usage_unavailable")
        self.assertEqual(row["raw_score"]["task_verdict"], "fail")
        self.assertIn("truncated", [x["code"] for x in row["raw_score"]["dimensions"]["delivery"]["checks"]])

    def test_timeout_deadline_not_only_transport_exception(self):
        row = self.single({"delay": 1.2}, limits={"request_seconds": 1})
        self.assertEqual(row["budget"]["stop_reason"], "model_timeout")
        self.assertIn("delay", row["record"]["fault_hits"])

    def test_duplicate_call_id_rejected(self):
        row = self.single({"duplicate_call_id": True}, tid="IC-07")
        self.assertEqual(row["budget"]["stop_reason"], "duplicate_tool_call_identity")
        self.assertEqual(len(row["record"]["events"]), 1)

    def test_full_denominator_on_first_failure(self):
        result = r.base.LOOP.run_until_complete(r.execute_batch("DENOM", faults={"IC-01": {"missing_usage": True}}))
        FAULT_REPORTS.append({"kind": "full_denominator_fault", "result": result})
        for path in result["scores"]:
            counts = result["scores"][path]["raw_report"]["counts"]
            self.assertEqual(counts["planned"], 16)
            self.assertEqual(counts["not_executed"], 15)
            self.assertEqual(len(result["records"][path]), 16)
        self.assertEqual(len(result["budget"]["request_attempts"]), 1)
        self.assertEqual(result["records"]["agent"]["IC-01"]["execution_state"], "observed")

    def test_source_rejection_before_start_marks_both_paths_not_executed(self):
        with patch.object(r, "check_snapshot", side_effect=StopRun("native_source_drift")), \
             patch.object(r, "fixed_path") as fixed, patch.object(r, "run_agent") as agent:
            result = r.base.LOOP.run_until_complete(r.execute_batch("PRESTART-SOURCE"))
        fixed.assert_not_called()
        agent.assert_not_called()
        FAULT_REPORTS.append({"kind": "prestart_admission_fault", "injection": "source_guard_rejection", "result": result})
        self.assertEqual(result["budget"]["request_attempts"], [])
        self.assertEqual(result["budget"]["stop_reason"], "native_source_drift")
        for path in result["records"]:
            self.assertEqual(result["scores"][path]["raw_report"]["counts"]["planned"], 16)
            self.assertEqual(result["scores"][path]["raw_report"]["counts"]["not_executed"], 16)
            self.assertEqual(result["scores"][path]["raw_report"]["counts"]["observed"], 0)
            for record in result["records"][path].values():
                self.assertEqual(record["execution_state"], "not_executed")
                self.assertIsNone(record["final_output"])
                self.assertEqual(record["events"], [])
                self.assertEqual(record["allowed_evidence"], [])

    def test_begin_task_rejection_keeps_prior_observation_and_full_denominator(self):
        original_begin = r.Budget.begin_task
        for error, reason in [(StopRun("task_deadline"), "task_deadline"),
                              (ValueError("fixture_prestart_failure"), "execution_exception")]:
            def reject_agent(budget, key):
                if key.endswith(":agent"): raise error
                return original_begin(budget, key)
            with self.subTest(reason=reason), patch.object(r.Budget, "begin_task", new=reject_agent), \
                 patch.object(r, "run_agent") as agent:
                result = r.base.LOOP.run_until_complete(r.execute_batch("PRESTART-BEGIN"))
            agent.assert_not_called()
            FAULT_REPORTS.append({"kind": "prestart_admission_fault", "injection": reason, "result": result})
            self.assertEqual(result["budget"]["stop_reason"], reason)
            self.assertEqual(result["budget"]["request_attempts"], [])
            self.assertEqual(result["records"]["fixed_workflow"]["IC-01"]["execution_state"], "observed")
            first = result["records"]["agent"]["IC-01"]
            self.assertEqual(first["execution_state"], "not_executed")
            self.assertIsNone(first["final_output"])
            self.assertEqual(first["events"], [])
            self.assertEqual(first["allowed_evidence"], [])
            for path in result["scores"]:
                self.assertEqual(result["scores"][path]["raw_report"]["counts"]["planned"], 16)
            self.assertEqual(result["scores"]["agent"]["raw_report"]["counts"]["not_executed"], 16)
            self.assertEqual(result["scores"]["fixed_workflow"]["raw_report"]["counts"]["not_executed"], 15)
            delivery = result["scores"]["agent"]["raw_report"]["tasks"][0]["dimensions"]["delivery"]
            self.assertEqual(delivery["status"], "fail")
            self.assertEqual([check["code"] for check in delivery["checks"]], ["not_executed"])

    def test_scoring_binding_drift_keeps_all_raw_observations(self):
        with patch.object(r.base, "score_records", side_effect=StopRun("scoring_contract_drift")):
            result = r.base.LOOP.run_until_complete(r.execute_batch("GOLD-DRIFT", faults={"IC-01": {"missing_usage": True}}))
        FAULT_REPORTS.append({"kind": "scoring_admission_fault_injection", "result": result,
                              "actual_scorer_invoked": False})
        self.assertFalse(result["actual_scorer_connected"])
        for path in result["records"]:
            self.assertEqual(len(result["records"][path]), 16)
            self.assertEqual(result["scores"][path]["error"]["code"], "scoring_contract_drift")
            self.assertIsNone(result["scores"][path]["raw_report"])

    def test_no_gold_in_either_path_and_inputs_unchanged(self):
        original_open = Path.open
        def guarded(path, *a, **kw):
            if "scoring" in path.parts: raise AssertionError("gold read")
            return original_open(path, *a, **kw)
        task = deepcopy(self.tasks[0]); script = deepcopy(self.scripts[task["task_id"]])
        before = deepcopy((task, script))
        with patch.object(Path, "open", guarded):
            for path in ("fixed_workflow", "agent"):
                b = Budget(self.limits)
                s = Session(task, path, "GOLD-" + path, b, r.source_snapshot())
                b.begin_task(s.key)
                r.base.LOOP.run_until_complete(fixed_path(s) if path == "fixed_workflow" else run_agent(s, script))
        self.assertEqual((task, script), before)

    def test_live_transport_key_and_online_mode_denied(self):
        with self.assertRaises(ValueError): build_client(object(), None, FAKE_KEY)
        with self.assertRaises(ValueError): build_client(httpx2.MockTransport(lambda r: None), None, "not-a-key")
        with self.assertRaisesRegex(StopRun, "real_execution_entry_not_approved"):
            r.base.LOOP.run_until_complete(r.execute_batch("DENIED", mode="online"))
        with self.assertRaises(PermissionError): socket.getaddrinfo("example.invalid", 443)
        with socket.socket() as sock:
            with self.assertRaises(PermissionError): sock.connect(("127.0.0.1", 9))

    def test_preservation_and_source_drift(self):
        self.assertTrue(all(all(group.values()) for group in r.preserved().values()))
        changed = r.snapshot(); changed["bogus.py"] = "bad"
        with self.assertRaisesRegex(StopRun, "native_source_drift"): r.check_snapshot(changed)

    def test_native_output_wrong_facts_units_direction_citation(self):
        cases = [("IC-01", "Spruce的质量为44.20 mg [E1]。", "facts", "value_mismatch"),
                 ("IC-01", "Spruce的质量为43.20 mm [E1]。", "facts", "unit_mismatch"),
                 ("IC-05", "East-West的差值为6.4 mg [E1]。", "facts", "direction_mismatch"),
                 ("IC-01", "Spruce的质量为43.20 mg [E99]。", "evidence", "evidence_not_allowed")]
        for tid, text, dimension, code in cases:
            with self.subTest(code=code, tid=tid):
                row = self.single(tid=tid, text_override=True, text=text)
                self.assertEqual(row["raw_score"]["task_verdict"], "fail")
                self.assertIn(code, [x["code"] for x in row["raw_score"]["dimensions"][dimension]["checks"]])

    def test_evidence_faults_rejected_by_actual_scorer(self):
        record = self.normal["records"]["agent"]["IC-01"]
        for fault, code in [("cross_run", "evidence_run_mismatch"), ("unproduced", "evidence_not_produced"),
                            ("failed_call", "evidence_not_produced")]:
            obs, evidence = r.base.observation_for_score(record)
            if fault == "cross_run": evidence[0]["run_id"] = "ANOTHER-RUN"
            elif fault == "unproduced": obs["events"][0]["produced_artifacts"] = []
            else:
                obs["events"][0]["status"] = "failed"
                obs["events"][0]["produced_artifacts"] = []
            report = r.base.SCORING.score_case(self.contracts["IC-01"], obs, evidence)
            FAULT_REPORTS.append({"kind": "postrun_scorer_fault", "fault": fault, "input": obs,
                                  "allowed_evidence": evidence, "raw_score": report})
            self.assertEqual(report["task_verdict"], "fail")
            self.assertIn(code, [x["code"] for x in report["dimensions"]["evidence"]["checks"]])

    def test_tool_failure_not_attributed_to_model(self):
        row = self.single({"tool_failure": "read_aggregate"})
        self.assertEqual(row["record"]["status"], "failed")
        self.assertEqual(row["record"]["allowed_evidence"], [])
        self.assertEqual(row["record"]["known_failures"][0]["attribution"], "source_or_tool_not_model_verdict")
        self.assertEqual(row["raw_score"]["task_verdict"], "fail")
        self.assertIn("tool_status_mismatch", [x["code"] for x in row["raw_score"]["dimensions"]["behavior"]["checks"]])

    def test_invalid_observation_raises_contract_error_no_fallback(self):
        obs, evidence = r.base.observation_for_score(self.normal["records"]["agent"]["IC-01"])
        obs["final_output"] = 42
        with self.assertRaises(r.base.SCORING.ContractError) as caught:
            r.base.SCORING.score_case(self.contracts["IC-01"], obs, evidence)
        FAULT_REPORTS.append({"kind": "invalid_input_rejection", "code": caught.exception.code,
                              "path": caught.exception.path, "input": obs})
        self.assertEqual(caught.exception.path, "/observation/final_output")

    def test_selection_matches_native_policy_and_fixed_identity(self):
        s = self.normal["selection"]
        self.assertEqual(s["api_model_id"], MODEL)
        self.assertFalse(s["online_authorized"])
        self.assertIsNone(s["execution_commit"])
        self.assertEqual(s["scorer_commit"], r.base.SCORER)
        self.assertEqual(s["fixture_request_policy"]["max_output_tokens"], 1000)
        self.assertEqual(s["fixture_request_policy"]["reasoning_effort"], "none")


def make_fault_test(fault, expected):
    def test(self):
        row = self.single(fault)
        self.assertEqual(row["budget"]["stop_reason"], expected)
        self.assertEqual(len(row["budget"]["request_attempts"]), 1)
        self.assertEqual(row["budget"]["unsettled_reservation_count"], 0)
        self.assertEqual(row["record"]["events"], [])
        self.assertTrue(set(fault).issubset(row["record"]["fault_hits"]))
        wire = row["record"]["native_wire_observation"]
        self.assertEqual(len(wire["fixture_requests"]), 1)  # no retry or redirect follow
        serialized = json.dumps(row)
        self.assertNotIn(FAKE_KEY, serialized)
        self.assertNotIn("PRIVATE_REASONING_SENTINEL", serialized)
    return test


for name, (fault, expected) in FAULTS.items():
    setattr(NativeTests, "test_fault_" + name, make_fault_test(fault, expected))
