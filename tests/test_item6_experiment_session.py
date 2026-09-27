import unittest
from . import item6_experiment_fixture as f


class SessionTests(unittest.TestCase):
    def test_function_call_completion_status_rejects_before_tools(self):
        for mode in ("function_incomplete", "function_status_missing", "function_status_null"):
            with self.subTest(mode=mode):
                result = f.run_case(mode)
                self.assertEqual(result["process_exit_code"], 2)
                self.assertIn(mode, result["hits"])
                self.assertEqual(len(result["calls"]), 1)
                artifact = result["artifact"]
                self.assertEqual(artifact["authority"]["stopped"], "item6_function_call_not_complete")
                row = artifact["business"]["agent"]["IC-01"]
                self.assertEqual(row["events"], [])
                self.assertEqual(row["allowed_evidence"], [])
                self.assertEqual(row["plan"], [])
                self.assertEqual(artifact["scores"]["agent"]["raw_report"]["counts"]["planned"], 16)

    def test_timed_metadata_and_usage_share_actual_event(self):
        artifact = f.normal()["artifact"]
        terminal = [e for e in artifact["audit"]["events"] if e["event_type"] == "model_response_telemetry_recorded"]
        self.assertEqual(len(terminal), 30)
        self.assertEqual(len(artifact["segments"]), 30)
        for event, segment in zip(terminal, artifact["segments"]):
            self.assertEqual(event["safe_payload"]["attempt_index"], segment["attempt_index"])
            self.assertEqual(event["safe_payload"]["timing"]["segment_commitment_sha256"], segment["segment_commitment_sha256"])

    def test_http_failures_and_transport_errors_never_retry(self):
        for mode in ("http401", "http429", "http503", "timeout", "cancel"):
            with self.subTest(mode=mode):
                result = f.run_case(mode)
                self.assertEqual(result["process_exit_code"], 2)
                self.assertEqual(len(result["calls"]), 1)
                self.assertIn(mode, result["hits"])
                self.assertEqual(result["network_attempts"], 0)

    def test_invalid_tool_reasoning_or_body_stops_new_dispatch(self):
        for mode in ("bad_tool", "bad_arguments", "reasoning", "oversize"):
            with self.subTest(mode=mode):
                result = f.run_case(mode)
                self.assertEqual(result["process_exit_code"], 2)
                self.assertEqual(len(result["calls"]), 1)
                self.assertIn(mode, result["hits"])

    def test_incomplete_response_not_normal_completion(self):
        result = f.run_case("truncated")
        self.assertEqual(result["process_exit_code"], 2)
        self.assertEqual(len(result["calls"]), 1)
        self.assertIn("truncated", result["hits"])

    def test_missing_null_and_unknown_status_are_not_completed(self):
        for mode in ("missing_status", "null_status", "unknown_status"):
            with self.subTest(mode=mode):
                result = f.run_case(mode)
                self.assertEqual(result["process_exit_code"], 2)
                self.assertEqual(len(result["calls"]), 1)
                self.assertIn(mode, result["hits"])

    def test_wrong_origin_or_live_delegate_in_test_mode_never_dispatches(self):
        for mode in ("wrong_origin", "offline_native_delegate"):
            with self.subTest(mode=mode):
                result = f.run_case(mode)
                self.assertEqual(result["process_exit_code"], 2)
                self.assertEqual(result["calls"], [])
                self.assertIn(mode, result["hits"])

    def test_start_and_send_intent_audit_failures_are_pre_dispatch(self):
        for mode in ("audit_start", "audit_intent"):
            with self.subTest(mode=mode):
                result = f.run_case(mode)
                self.assertEqual(result["process_exit_code"], 2)
                self.assertEqual(result["calls"], [])
                self.assertIn(mode, result["hits"])

    def test_unknown_outcome_cannot_be_retried(self):
        result = f.run_case("after_unknown_retry")
        self.assertEqual(result["post_checks"]["calls_before_retry"], 1)
        self.assertEqual(result["post_checks"]["calls_after_retry"], 1)
        self.assertEqual(result["post_checks"]["retry_error"], "item6_owner_stopped")

    def test_tool_audit_failures_stop_dispatch_and_do_not_grant_evidence(self):
        for mode, stage, reads in (("audit_tool_start", "start", 0), ("audit_tool_finish", "finish", 1)):
            with self.subTest(mode=mode):
                result = f.run_case(mode)
                self.assertEqual(result["process_exit_code"], 2, result.get("error"))
                self.assertIn(mode, result["hits"])
                self.assertEqual(result["hits"].count("agent_tool_read_observed"), reads)
                self.assertEqual(len(result["calls"]), 1)
                artifact = result["artifact"]
                code = "item6_tool_" + stage + "_audit_failed"
                self.assertEqual(artifact["authority"]["stopped"], code)
                self.assertEqual(artifact["status"], "failed")
                row = artifact["business"]["agent"]["IC-01"]
                self.assertFalse(row["events_complete"])
                self.assertEqual(row["allowed_evidence"], [])
                self.assertEqual(row["events"][0]["status"], "failed")
                self.assertEqual(row["events"][0]["produced_artifacts"], [])
                self.assertTrue(any(f["code"] == code for f in row["known_failures"]))
                self.assertEqual(row["plan"][0]["execution"], "not_executed" if stage == "start" else "executed")
                if stage == "start": self.assertIsNone(row["events"][0]["result"])
                else: self.assertIn("facts", row["events"][0]["result"])
                for path in ("agent", "fixed_workflow"):
                    self.assertEqual(len(artifact["business"][path]), 16)
                    self.assertEqual(artifact["scores"][path]["raw_report"]["counts"]["planned"], 16)
                scored = artifact["scores"]["agent"]["raw_report"]["tasks"][0]
                self.assertEqual(scored["task_verdict"], "fail")
                behavior = scored["dimensions"]["behavior"]
                self.assertEqual(behavior["status"], "fail")
                self.assertTrue(any(c["code"] == "tool_status_mismatch" and c["status"] == "fail" for c in behavior["checks"]))
                self.assertTrue(any(c["code"] == "event_coverage_incomplete" and c["status"] == "unknown" for c in behavior["checks"]))
                f.RESULTS.append(dict(kind="tool_audit_boundary", mode_label=mode, dispatches=1, actual_exit_code=2,
                    agent_reads=reads, stop_code=code, allowed_evidence_count=0, events_complete=False,
                    behavior_fail_code="tool_status_mismatch", behavior_unknown_code="event_coverage_incomplete"))

    def test_duplicate_call_ids_stop_before_tool_execution(self):
        result = f.run_case("duplicate_call")
        self.assertEqual(result["process_exit_code"], 2, result.get("error"))
        self.assertIn("duplicate_call", result["hits"])
        self.assertIn("duplicate_in_single_response", result["hits"])
        self.assertEqual(len(result["calls"]), 1)
        artifact = result["artifact"]
        self.assertEqual(artifact["authority"]["stopped"], "item6_call_identity")
        row = artifact["business"]["agent"]["IC-01"]
        self.assertEqual(row["events"], [])
        self.assertEqual(row["allowed_evidence"], [])
        self.assertTrue(all(step["execution"] == "not_executed" for step in row["plan"]))
        self.assertEqual(artifact["scores"]["agent"]["raw_report"]["counts"]["planned"], 16)
        self.assertEqual(artifact["scores"]["agent"]["raw_report"]["tasks"][0]["task_verdict"], "fail")
