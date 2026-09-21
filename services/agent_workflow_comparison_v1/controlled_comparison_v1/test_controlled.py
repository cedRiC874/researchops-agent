from copy import deepcopy
import json
import io
import os
from pathlib import Path
import socket
import unittest
from unittest.mock import patch
import uuid

import httpx

from . import runtime
from .binding import ROOT, source_snapshot, verify_snapshot, reserve_output, seal, check_seal, digest
from .budget import Budget, StopRun
from .paths import Session, fixed_path, read_json, FAKE_KEY
from .mock_model import agent_path, client_for
from .human_record import HumanReview, unobserved_review


class TestClock:
    source = "test_fixture"
    def __init__(self): self.value = 0.0
    def now(self): return self.value


class ControlledTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.spec = read_json(ROOT / "experiment_spec.json")
        cls.tasks = read_json(ROOT / "tasks/frozen/tasks.json")["tasks"]
        cls.scripts = read_json(ROOT / "fixtures/development/mock_responses.json")
        cls.result = runtime.LOOP.run_until_complete(runtime.execute_batch("TESTNORMAL"))

    def batch(self, **kwargs):
        return runtime.LOOP.run_until_complete(runtime.execute_batch("TESTFAULT", **kwargs))

    def test_new_16_task_coverage_and_32_real_local_executions(self):
        self.assertEqual(len(self.tasks), 16)
        self.assertEqual(len(self.result["execution_order"]), 32)
        self.assertEqual(self.result["budget"]["real_provider_requests"], 0)
        self.assertIsNone(self.result["budget"]["stop_reason"])
        for path in ("fixed_workflow", "agent"):
            self.assertEqual(len(self.result["records"][path]), 16)
            self.assertEqual(self.result["scores"][path]["raw_report"]["counts"],
                {"planned": 16, "pass": 16, "fail": 0, "unknown": 0, "observed": 16, "not_executed": 0, "observation_missing": 0})
        self.assertFalse(self.result["formal_comparison_result"])

    def test_pair_order_alternates_and_inputs_match(self):
        for index, task in enumerate(self.tasks):
            paths = [x["path"] for x in self.result["execution_order"][index * 2:index * 2 + 2]]
            self.assertEqual(paths, ["fixed_workflow", "agent"] if index % 2 == 0 else ["agent", "fixed_workflow"])
            a, f = (self.result["records"][p][task["task_id"]] for p in ("agent", "fixed_workflow"))
            self.assertEqual(a["input_sha256"], f["input_sha256"])
            self.assertEqual(a["task_input"], f["task_input"])

    def test_rules_use_metadata_and_zero_tool_stop_branches(self):
        for number in range(7, 11):
            r = self.result["records"]["fixed_workflow"][f"IC-{number:02}"]
            self.assertEqual([e["tool"] for e in r["events"]], ["inspect_sources", "read_aggregate"])
            self.assertEqual(r["events"][1]["arguments"]["bundle_id"], f"aggregate-{number:02}")
        for number in range(11, 17):
            for path in ("fixed_workflow", "agent"):
                self.assertEqual(self.result["records"][path][f"IC-{number:02}"]["events"], [])

    def test_actual_mock_wire_requests_and_no_fake_api_usage(self):
        self.assertEqual(len(self.result["budget"]["request_attempts"]), 30)
        self.assertTrue(all(r["usage_origin"] == "synthetic_mock_response" for r in self.result["budget"]["request_attempts"]))
        for record in self.result["records"]["agent"].values():
            self.assertIsNone(record["actual_api_tokens"])
            self.assertIsNone(record["actual_api_cost"])
            self.assertEqual(record["model_response_origin"], "synthetic_mock_response")
        self.assertNotIn(FAKE_KEY, json.dumps(self.result))

    def test_evidence_matches_actual_successful_events(self):
        for rows in self.result["records"].values():
            for r in rows.values():
                for evidence in r["allowed_evidence"]:
                    event = next(e for e in r["events"] if e["call_id"] == evidence["call_id"])
                    self.assertEqual(event["status"], "succeeded")
                    self.assertEqual(evidence["run_id"], r["run_id"])
                    self.assertIn(evidence["artifact_id"], event["produced_artifacts"])
                    self.assertEqual(evidence["facts"], event["result"]["facts"])
                    self.assertEqual(evidence["content_sha256"], digest(event["result"]))

    def test_no_gold_reads_in_decision_paths_and_input_unchanged(self):
        task = deepcopy(self.tasks[0]); script = deepcopy(self.scripts[task["task_id"]]); original = deepcopy((task, script))
        snapshot = source_snapshot(); budget = Budget(self.spec["limits"])
        old_open = Path.open
        def guarded(path, *a, **kw):
            if "scoring" in path.parts:
                raise AssertionError("gold read by decision")
            return old_open(path, *a, **kw)
        with patch.object(Path, "open", guarded):
            for path in ("fixed_workflow", "agent"):
                session = Session(task, path, "ISOLATED-" + path, budget, snapshot)
                budget.begin_task(session.key)
                runtime.LOOP.run_until_complete(fixed_path(session) if path == "fixed_workflow" else agent_path(session, script))
        self.assertEqual((task, script), original)
        with self.assertRaises(ValueError):
            Session({**task, "expected_answer": "hidden"}, "fixed_workflow", "x", budget, snapshot)

    def test_real_mode_and_non_mock_transport_rejected(self):
        with self.assertRaisesRegex(StopRun, "real_execution_entry_not_approved"):
            self.batch(mode="online")
        with self.assertRaises(ValueError):
            client_for(object(), FAKE_KEY)
        with self.assertRaises(ValueError):
            client_for(httpx.MockTransport(lambda r: httpx.Response(200)), "not-the-fake-key")

    def test_request_token_byte_cost_and_concurrency_pre_send_boundaries(self):
        cases = [({"model_requests": 0}, 100, 100, "request_limit"),
                 ({"requests_per_task": 0}, 100, 100, "request_limit"),
                 ({"input_per_request": 10}, 11, 11, "input_bound_limit"),
                 ({"input_total": 10}, 11, 11, "token_reservation_limit"),
                 ({"output_total": 999}, 10, 10, "token_reservation_limit"),
                 ({"request_bytes": 10}, 9, 11, "request_byte_limit"),
                 ({"synthetic_cost_limit": "0.00001"}, 100, 100, "synthetic_cost_pre_send_limit")]
        for changes, bound, wire, reason in cases:
            budget = Budget({**self.spec["limits"], **changes})
            budget.begin_task("k")
            with self.subTest(reason=reason), self.assertRaisesRegex(StopRun, reason):
                budget.reserve("k", bound, wire)
            self.assertEqual(budget.requests, [])
        b = Budget(self.spec["limits"]); b.begin_task("k"); b.reserve("k", 100, 100)
        with self.assertRaisesRegex(StopRun, "concurrency_limit"):
            b.reserve("k", 100, 100)

    def test_unknown_usage_stops_without_cost_zero_or_denominator_loss(self):
        r = self.batch(faults={"IC-01": {"missing_usage": True}})
        self.assertEqual(r["budget"]["stop_reason"], "usage_unavailable")
        self.assertEqual(len(r["budget"]["request_attempts"]), 1)
        self.assertIsNone(r["budget"]["synthetic_cost_units"])
        self.assertIn("missing_usage", r["records"]["agent"]["IC-01"]["fault_hits"])
        for path in r["scores"]:
            self.assertEqual(r["scores"][path]["raw_report"]["counts"]["planned"], 16)
            self.assertEqual(r["scores"][path]["raw_report"]["counts"]["not_executed"], 15)

    def test_output_usage_overshoot_one_request_then_stop(self):
        r = self.batch(faults={"IC-01": {"usage_overrun": True}})
        self.assertEqual(r["budget"]["stop_reason"], "reported_usage_exceeds_reservation")
        self.assertEqual(len(r["budget"]["request_attempts"]), 1)
        self.assertEqual(r["budget"]["request_attempts"][0]["usage"]["output_tokens"], 1001)
        self.assertIn("usage_overrun", r["records"]["agent"]["IC-01"]["fault_hits"])

    def test_model_timeout_cancel_and_deadline_injections_hit(self):
        for fault, reason in [("transport_timeout", "model_timeout"), ("cancel", "cancelled")]:
            r = self.batch(faults={"IC-01": {fault: True}})
            self.assertEqual(r["budget"]["stop_reason"], reason)
            self.assertEqual(len(r["budget"]["request_attempts"]), 1)
            self.assertIn(fault, r["records"]["agent"]["IC-01"]["fault_hits"])
        r = self.batch(faults={"IC-01": {"delay": 0.02}}, limits_override={"request_seconds": 0.005})
        self.assertEqual(r["budget"]["stop_reason"], "model_timeout")
        self.assertIn("delay", r["records"]["agent"]["IC-01"]["fault_hits"])

    def test_tool_timeout_privacy_and_explicit_read_error(self):
        r = self.batch(faults={"IC-01": {"tool_timeout": True}}, limits_override={"tool_seconds": 0.005})
        self.assertEqual(r["budget"]["stop_reason"], "tool_timeout")
        self.assertIn("tool_timeout", r["records"]["fixed_workflow"]["IC-01"]["fault_hits"])
        r = self.batch(faults={"IC-01": {"privacy_leak": True}})
        self.assertEqual(r["budget"]["stop_reason"], "privacy_boundary")
        self.assertNotIn(FAKE_KEY, json.dumps(r))
        r = self.batch(faults={"IC-01": {"tool_failure": "read_aggregate"}})
        self.assertIsNone(r["budget"]["stop_reason"])
        self.assertEqual(r["records"]["fixed_workflow"]["IC-01"]["status"], "failed")
        self.assertIn("tool_failure", r["records"]["fixed_workflow"]["IC-01"]["fault_hits"])

    def test_model_response_privacy_rejection_does_not_leak(self):
        r = self.batch(faults={"IC-01": {"privacy_response": True}})
        self.assertEqual(r["budget"]["stop_reason"], "privacy_boundary")
        self.assertIn("privacy_response", r["records"]["agent"]["IC-01"]["fault_hits"])
        self.assertNotIn(FAKE_KEY, json.dumps(r))

    def test_clock_limits_and_duplicate_task(self):
        clock = TestClock(); b = Budget(self.spec["limits"], clock); b.begin_task("k")
        clock.value = 120
        with self.assertRaisesRegex(StopRun, "task_deadline"): b.check("k")
        b = Budget(self.spec["limits"], clock); clock.value += 2700
        with self.assertRaisesRegex(StopRun, "batch_deadline"): b.check()
        b = Budget(self.spec["limits"]); b.begin_task("k")
        with self.assertRaisesRegex(StopRun, "duplicate_task_execution"): b.begin_task("k")

    def test_tool_budget_scope_and_catalog_precondition(self):
        task = next(t for t in self.tasks if t["task_id"] == "IC-07")
        for tool, args, reason in [("read_aggregate", {"bundle_id": "aggregate-07"}, "catalog_precondition_missing"),
                                   ("inspect_sources", {"scope_id": "s08"}, "unauthorized_tool_scope")]:
            b = Budget(self.spec["limits"]); s = Session(task, "agent", "SCOPE", b, source_snapshot()); b.begin_task(s.key)
            with self.assertRaisesRegex(StopRun, reason): runtime.LOOP.run_until_complete(s.call(tool, args))
            self.assertEqual(s.record["allowed_evidence"], [])
        b = Budget({**self.spec["limits"], "tools_per_task": 0}); b.begin_task("k")
        with self.assertRaisesRegex(StopRun, "tool_budget"): b.tool("k")

    def test_missing_final_text_is_not_empty_and_is_scored_unknown(self):
        r = self.batch(faults={"IC-01": {"missing_text": True}})
        obs = r["records"]["agent"]["IC-01"]
        self.assertIsNone(obs["final_output"])
        self.assertEqual(obs["text_observation"], "unobserved")
        self.assertIn("missing_text", obs["fault_hits"])
        report = r["scores"]["agent"]["raw_report"]["tasks"][0]
        self.assertEqual(report["task_verdict"], "unknown")
        self.assertIn("final_output_unobserved", {c["code"] for c in report["dimensions"]["delivery"]["checks"]})

    def test_empty_and_whitespace_survive_sdk_and_projection(self):
        for text in ("", " \t\n　"):
            task = self.tasks[0]; script = deepcopy(self.scripts[task["task_id"]]); script["steps"][-1]["text"] = text
            b = Budget(self.spec["limits"]); s = Session(task, "agent", "EMPTY", b, source_snapshot()); b.begin_task(s.key)
            r = runtime.LOOP.run_until_complete(agent_path(s, script))
            self.assertEqual(r["final_output"], text)
            self.assertEqual(runtime.observation_for_score(r)[0]["final_output"], text)

    def test_source_and_scoring_contract_drift_are_rejected(self):
        snapshot = source_snapshot()
        with patch("services.agent_workflow_comparison_v1.controlled_comparison_v1.binding.source_snapshot", return_value={}):
            with self.assertRaisesRegex(StopRun, "source_drift"): verify_snapshot(snapshot)
        with self.assertRaisesRegex(StopRun, "scoring_contract_drift"):
            runtime.score_records(self.tasks, self.result["records"], "wrong")

    def test_ownership_and_artifact_tampering(self):
        run_id = "LOCK-" + uuid.uuid4().hex[:12]
        path = reserve_output(ROOT / "outputs" / run_id, run_id, source_snapshot())
        with self.assertRaises(FileExistsError): reserve_output(path, run_id, source_snapshot())
        with self.assertRaises(ValueError): reserve_output(ROOT / "outputs" / (run_id + "x"), run_id, source_snapshot())
        (path / "a.json").write_text('{"value":1}', encoding="utf-8")
        sealed = seal(path, ["a.json"])
        self.assertTrue(check_seal(path, sealed["manifest_sha256"]))
        (path / "a.json").write_text('{"value":2}', encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "artifact_tampering"): check_seal(path, sealed["manifest_sha256"])
        (path / "sealed.json").write_text('{"files":{}}', encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "seal_manifest_tampering"): check_seal(path, sealed["manifest_sha256"])

    def test_real_human_timer_separate_from_unobserved_review(self):
        r = unobserved_review("IC-01")
        self.assertIsNone(r["elapsed_seconds"])
        original = deepcopy(self.result["records"]["agent"]["IC-01"])
        timer = HumanReview("IC-01", "test-reviewer")
        measured = timer.finish(needs_correction=False, edits=[], followup_design_required=False)
        self.assertGreaterEqual(measured["elapsed_seconds"], 0)
        self.assertFalse(measured["changes_original_score"])
        self.assertEqual(original, self.result["records"]["agent"]["IC-01"])

    def test_network_denied_and_no_real_key_environment_access(self):
        with socket.socket() as sock:
            with self.assertRaises(PermissionError): sock.connect(("192.0.2.1", 443))
        with self.assertRaises(PermissionError): socket.getaddrinfo("example.invalid", 443)
        get = type(os.environ).__getitem__
        def guard(env, key):
            if "KEY" in key.upper() or "SECRET" in key.upper(): raise AssertionError("real key access")
            return get(env, key)
        with patch.object(type(os.environ), "__getitem__", guard):
            r = self.batch(tasks_override=self.tasks[:1])
        self.assertIsNone(r["budget"]["stop_reason"])

    def test_negative_fabrication_instruction_is_not_a_fabrication_request(self):
        task = deepcopy(self.tasks[0]); task["request"] += "不要伪造。"
        b = Budget(self.spec["limits"]); s = Session(task, "fixed_workflow", "NEGATION", b, source_snapshot()); b.begin_task(s.key)
        r = runtime.LOOP.run_until_complete(fixed_path(s))
        self.assertEqual(r["status"], "completed")
        self.assertEqual(len(r["events"]), 1)

    def test_scoring_drift_preserves_observations_and_stops_without_fake_report(self):
        with patch.object(runtime, "score_records", side_effect=StopRun("scoring_contract_drift")):
            result = self.batch()
        self.assertEqual(result["budget"]["stop_reason"], "scoring_contract_drift")
        self.assertEqual(len(result["records"]["agent"]), 16)
        self.assertIsNone(result["scores"]["agent"]["raw_report"])
        self.assertEqual(result["scores"]["agent"]["error"]["code"], "scoring_contract_drift")

    def test_unknown_cli_arguments_never_echo_a_key(self):
        from .run import SafeParser
        parser = SafeParser(); parser.add_argument("--run-id")
        captured = io.StringIO()
        with patch("sys.stderr", captured), self.assertRaises(SystemExit) as error:
            parser.parse_args(["--api-key", FAKE_KEY])
        self.assertEqual(error.exception.code, 2)
        self.assertNotIn(FAKE_KEY, captured.getvalue())

    def test_human_records_reject_identifiers_and_secret_edits(self):
        with self.assertRaises(ValueError): HumanReview("IC-01", "name@example.invalid")
        timer = HumanReview("IC-01", "reviewer-01")
        with self.assertRaises(StopRun):
            timer.finish(needs_correction=True, edits=[{"replacement": FAKE_KEY}], followup_design_required=False)

    def test_private_input_in_later_row_is_rejected_before_any_path_starts(self):
        tasks = deepcopy(self.tasks)
        tasks[-1]["request"] += FAKE_KEY
        with patch.object(runtime, "fixed_path") as fixed, patch.object(runtime, "agent_path") as agent:
            with self.assertRaisesRegex(StopRun, "privacy_boundary"):
                self.batch(tasks_override=tasks)
        fixed.assert_not_called()
        agent.assert_not_called()
