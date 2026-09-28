"""Offline-only rejection identity, closure, disclosure and real SDK regressions."""
from copy import deepcopy
import json
from types import SimpleNamespace
import unittest

from researchops_item6_experiment_v1 import case_isolation as isolation, contract as c
from . import item6_experiment_fixture as f


class IsolationUnitTests(unittest.TestCase):
    def setUp(self):
        self.tasks = {t["task_id"]: t for t in json.loads((f.ROOT / c.TASKS).read_bytes())["tasks"]}
        self.evidence = json.loads((f.ROOT / c.EVIDENCE).read_bytes())

    def test_missing_and_conflicting_design_are_generic_predicates(self):
        for tid, reason in (("IC-11", "missing_design"), ("IC-13", "conflicting_design")):
            task = self.tasks[tid]
            self.assertEqual(isolation.reason(task, "inspect_sources", {"scope_id": task["scope_id"]}, self.evidence), reason)

    def test_valid_design_without_refusal_is_excluded(self):
        for tid in ("IC-01", "IC-02"):
            task = self.tasks[tid]
            self.assertIsNone(isolation.reason(task, "inspect_sources", {"scope_id": task["scope_id"]}, self.evidence))

    def test_scope_unknown_tool_and_extra_arguments_are_excluded(self):
        task = self.tasks["IC-11"]
        for tool, args in (("inspect_sources", {"scope_id": "outside"}), ("publish", {}),
                           ("inspect_sources", {"scope_id": task["scope_id"], "extra": True})):
            self.assertIsNone(isolation.reason(task, tool, args, self.evidence))

    def test_missing_catalog_cannot_be_created_by_eligibility(self):
        self.assertIsNone(isolation.reason(self.tasks["IC-11"], "read_aggregate", {"bundle_id": "aggregate-01"}, self.evidence))

    def test_forged_factory_is_not_a_capability(self):
        with self.assertRaisesRegex(c.ExperimentError, "isolation_factory"):
            isolation.bind_factory(SimpleNamespace())

    def test_matching_code_without_registered_original_object_is_not_authority(self):
        from researchops_item6_experiment_v1.observations import Case
        from agents.exceptions import UserError
        case = object.__new__(Case)
        error = c.ExperimentError("tool_before_design_or_refusal")
        wrapper = UserError("synthetic")
        wrapper.__cause__ = error
        self.assertFalse(isolation.capture(case, error))
        self.assertFalse(isolation.capture(case, wrapper))
        case.record = {"case_rejection": {"state": "isolated", "code": isolation.CODE}}
        self.assertFalse(isolation.capture(case, wrapper))

    def test_new_event_privacy_and_size_checks_run_before_any_ledger_write(self):
        from researchops_external_closure.errors import ExternalClosurePrimitiveError
        writes = []
        class Ledger:
            def append_event(self, *args, **kwargs):
                writes.append(True)
                raise AssertionError("sensitive payload reached the ledger")
        case = SimpleNamespace(factory=SimpleNamespace(canary="offline-unit-canary", ledger=Ledger(),
                                                        owner=SimpleNamespace(run_id="synthetic")))
        for value in ("offline-unit-canary", "Authorization: synthetic", "C:/Users/example/private.txt", "Traceback: synthetic"):
            with self.subTest(value=value), self.assertRaises((c.ExperimentError, ExternalClosurePrimitiveError)):
                isolation._append(case, isolation.REJECT, {"field": value})
        with self.assertRaisesRegex(c.ExperimentError, "business_size"):
            isolation._append(case, isolation.REJECT, {"field": "x" * (c.policy()["business_field_bytes"] + 1)})
        self.assertEqual(writes, [])

    @staticmethod
    def reconciliation():
        return dict(case_id="case", attempts_started=1, attempts_terminal=1, observed_response_count=1,
            accepted_response_count=1, rejected_response_count=0, sdk_raw_response_count=1,
            sdk_raw_response_reconciliation="matched", sdk_usage_request_count=1,
            sdk_usage_request_reconciliation="matched", closure_eligible=True,
            sdk_request_usage_indices_by_response=[dict(response_index=4, sdk_raw_response_index=0, sdk_request_usage_indices=[])])

    def test_native_empty_sdk_usage_entries_remain_empty_not_fabricated(self):
        value = self.reconciliation(); before = deepcopy(value)
        isolation._reconciliation(value, "case", [dict(response_index=4)])
        self.assertEqual(value, before)

    def test_missing_mismatched_or_noninteger_reconciliation_is_rejected(self):
        for key, value in (("sdk_request_usage_indices_by_response", []), ("sdk_usage_request_count", 0),
                           ("attempts_terminal", True), ("accepted_response_count", 1.0), ("closure_eligible", False)):
            row = self.reconciliation(); row[key] = value
            with self.subTest(field=key), self.assertRaises(c.ExperimentError):
                isolation._reconciliation(row, "case", [dict(response_index=4)])
        row = self.reconciliation(); row["sdk_request_usage_indices_by_response"][0]["sdk_request_usage_indices"] = [1]
        with self.assertRaises(c.ExperimentError): isolation._reconciliation(row, "case", [dict(response_index=4)])

    @staticmethod
    def collection():
        records = {path: {str(i): dict(execution_state="observed", status="completed", completion="complete", case_rejection=None)
                          for i in range(16)} for path in ("agent", "fixed_workflow")}
        scores = {path: dict(error=None, raw_report=dict(tasks=[dict(task_verdict="pass") for _ in range(16)])) for path in records}
        return records, scores

    def test_clean_collection_is_not_assumed_without_fcc(self):
        records, scores = self.collection()
        value = isolation.result_fields(records, scores, None)
        self.assertEqual(value["collection_status"], "complete")
        self.assertTrue(value["all_business_passed"])
        scores["agent"]["raw_report"]["tasks"][0]["task_verdict"] = "unknown"
        self.assertFalse(isolation.result_fields(records, scores, None)["all_business_passed"])

    def test_isolated_rejection_is_failed_observation_not_not_executed(self):
        records, scores = self.collection()
        records["agent"]["0"].update(status="failed", completion="unknown", case_rejection={"state": "isolated"})
        value = isolation.result_fields(records, scores, None)
        self.assertEqual(value["collection_status"], "complete_with_case_rejections")
        self.assertEqual(value["collection_summary"], dict(planned=32, observed=32, not_executed=0,
            operational_complete=31, failed_after_start=1, isolated_case_rejections=1))
        self.assertFalse(value["all_business_passed"])

    def test_stopped_or_unexecuted_collection_cannot_be_complete(self):
        records, scores = self.collection()
        self.assertEqual(isolation.result_fields(records, scores, "item6_source_drift")["collection_status"], "stopped")
        records["agent"]["0"].update(execution_state="not_executed", status="failed", completion="unknown")
        value = isolation.result_fields(records, scores, None)
        self.assertEqual(value["collection_status"], "stopped")
        self.assertFalse(value["all_business_passed"])

    def test_scoring_error_is_not_a_model_pass_or_unknown(self):
        records, scores = self.collection()
        scores["agent"] = dict(error={"code": "synthetic_contract_error"}, raw_report=None)
        self.assertFalse(isolation.result_fields(records, scores, None)["all_business_passed"])


class IsolationIntegrationTests(unittest.TestCase):
    def test_actual_sdk_complete_denominator_with_two_isolated_rejections(self):
        result = f.isolation_case()
        self.assertEqual(result["process_exit_code"], 3, result.get("error"))
        self.assertEqual((result["claim_files"], result["provider_calls"], result["network_attempts"]), (1, 0, 0))
        self.assertFalse(result["real_store_touched"])
        self.assertEqual(len(result["calls"]), 30)
        document = result["artifact"]
        self.assertEqual(document["schema_version"], c.ARTIFACT_VERSION)
        self.assertEqual(document["status"], "completed")
        self.assertEqual(document["collection_status"], "complete_with_case_rejections")
        self.assertEqual(document["collection_summary"], dict(planned=32, observed=32, not_executed=0,
            operational_complete=30, failed_after_start=2, isolated_case_rejections=2))
        self.assertFalse(document["all_business_passed"])
        self.assertIsNone(document["authority"]["stopped"])
        self.assertIsNone(document["phase"]["active_attempt"])
        self.assertEqual(document["denominator"]["not_finalized_case_ids"], [])
        events = document["audit"]["events"]
        for event, count in ((isolation.START, 32), (isolation.CLOSE, 32), (isolation.REJECT, 2), (isolation.SEALED, 2)):
            self.assertEqual(sum(row["event_type"] == event for row in events), count)
        for tid in ("IC-11", "IC-13"):
            self.assertEqual(result["hits"].count("isolation_response_" + tid), 1)
            self.assertNotIn("target_tool_read_" + tid, result["hits"])
            row = document["business"]["agent"][tid]
            self.assertEqual((row["execution_state"], row["status"], row["completion"]), ("observed", "failed", "unknown"))
            self.assertIsNone(row["final_output"])
            self.assertEqual(row["events"], [])
            self.assertEqual(row["allowed_evidence"], [])
            self.assertEqual(row["plan"][0]["execution"], "not_executed")
            self.assertEqual(row["case_rejection"]["state"], "isolated")
            self.assertTrue(row["case_rejection"]["provider_closed"])
            self.assertTrue(row["case_rejection"]["reconciliation"]["closure_eligible"])
        checked = f.verify_result(result)
        self.assertEqual(checked["actual_exit_code"], 0, checked)
        self.assertTrue(checked["archive_verified"] and checked["execution_completed"])
        self.assertEqual(checked["collection_status"], "complete_with_case_rejections")
        self.assertFalse(checked["all_business_passed"] or checked["runtime_authority_granted"])
        self.assertEqual(checked["unacknowledged_case_events"], [])

    def assert_stopped(self, mode):
        result = f.isolation_case(mode)
        self.assertEqual(result["process_exit_code"], 2, result.get("error"))
        self.assertEqual((result["provider_calls"], result["network_attempts"]), (0, 0))
        self.assertFalse(result["real_store_touched"])
        self.assertEqual(result["artifact"]["collection_status"], "stopped")
        self.assertFalse(result["artifact"]["all_business_passed"])
        last = "IC-14" if mode == "isolation_refusal" else "IC-11"
        self.assertEqual(result["calls"][-1]["task_id"], last)
        self.assertEqual(sum(row["task_id"] == last for row in result["calls"]), 1)
        self.assertNotIn("target_tool_read_" + last, result["hits"])
        return result

    def test_invalid_scope_and_catalog_are_still_batch_stops(self):
        for mode in ("isolation_scope", "isolation_catalog"):
            with self.subTest(mode=mode):
                result = self.assert_stopped(mode)
                tid = result["calls"][-1]["task_id"]
                self.assertIsNone(result["artifact"]["business"]["agent"][tid]["case_rejection"])

    def test_unregistered_replayed_closure_or_send_cannot_continue(self):
        for mode in ("isolation_capture_clone", "isolation_repeat_capture", "isolation_forged_reconciliation",
                     "isolation_missing_indices", "isolation_send_after_reject"):
            with self.subTest(mode=mode):
                result = self.assert_stopped(mode)
                self.assertEqual(result["hits"].count(mode), 1)

    def test_real_context_close_failure_or_cancellation_stops(self):
        for mode in ("isolation_provider_close", "isolation_provider_cancel"):
            with self.subTest(mode=mode):
                result = self.assert_stopped(mode)
                self.assertGreaterEqual(result["hits"].count(mode), 1)
                self.assertIn("original_transport_closed", result["hits"])
                self.assertFalse(result["artifact"]["business"]["agent"]["IC-11"]["case_rejection"]["provider_closed"])

    def test_consumed_original_error_cannot_isolate_another_case(self):
        result = f.isolation_case("isolation_cross_case_error")
        self.assertEqual(result["process_exit_code"], 2)
        self.assertEqual(result["hits"].count("isolation_cross_case_error"), 1)
        self.assertEqual(result["calls"][-1]["task_id"], "IC-13")
        self.assertEqual(result["artifact"]["business"]["agent"]["IC-11"]["case_rejection"]["state"], "isolated")
        self.assertEqual(result["artifact"]["business"]["agent"]["IC-13"]["case_rejection"]["state"], "rejected")
        self.assertEqual(result["artifact"]["collection_status"], "stopped")

    def test_audit_before_and_committed_but_unacknowledged_failures_stop(self):
        for when in ("before", "after"):
            for event in ("reject", "close", "seal"):
                mode = "isolation_audit_" + when + "_" + event
                with self.subTest(mode=mode):
                    result = self.assert_stopped(mode)
                    self.assertEqual(result["hits"].count(mode), 1)
                    self.assertEqual(result["hits"].count("original_append_committed_before_failure"), int(when == "after"))
                    checked = f.verify_result(result)
                    self.assertEqual(checked["actual_exit_code"], 0, checked)
                    self.assertTrue(checked["archive_verified"])
                    self.assertFalse(checked["execution_completed"])
                    self.assertEqual(len(checked["unacknowledged_case_events"]), int(when == "after"))

    def test_old_or_drifted_policy_freeze_cannot_claim(self):
        for mode in ("old_freeze", "isolation_policy_drift"):
            with self.subTest(mode=mode):
                result = f.isolation_case(mode)
                self.assertEqual(result["process_exit_code"], 2)
                self.assertEqual(result["calls"], [])
                self.assertEqual(result["claim_files"], 0)
                self.assertIn(mode, result["hits"])
                self.assertEqual(result["error"], "item6_freeze_schema" if mode == "old_freeze" else "item6_case_rejection_policy_binding")

    def test_source_and_absolute_expiry_still_stop_after_registered_rejection(self):
        for mode in ("isolation_source_drift", "isolation_expired"):
            with self.subTest(mode=mode):
                result = f.isolation_case(mode)
                self.assertEqual(result["process_exit_code"], 2)
                self.assertEqual(result["hits"].count(mode), 1)
                self.assertEqual(result["calls"][-1]["task_id"], "IC-11")
                self.assertEqual(sum(row["task_id"] == "IC-11" for row in result["calls"]), 1)
                self.assertEqual((result["provider_calls"], result["network_attempts"]), (0, 0))
                if mode == "isolation_source_drift":
                    self.assertEqual(result["artifact"]["authority"]["stopped"], "internal_source_v3_source_drift")
                    self.assertEqual(result["artifact"]["business"]["agent"]["IC-11"]["case_rejection"]["state"], "rejected")
                else:
                    self.assertEqual(result["primary_stop_reason"], "item6_approval_window")
                    self.assertIsNone(result["run"])

    def test_later_unfinished_attempt_keeps_prior_isolation_without_completion_claim(self):
        result = f.isolation_case("isolation_later_unfinished")
        self.assertEqual(result["process_exit_code"], 2, result.get("error"))
        self.assertIn("isolation_later_unfinished", result["hits"])
        self.assertEqual(result["calls"][-1]["task_id"], "IC-14")
        self.assertEqual(result["run"]["archive_kind"], "unfinished_failure")
        self.assertEqual(result["artifact"]["collection_status"], "stopped")
        self.assertEqual(result["artifact"]["business"]["agent"]["IC-11"]["case_rejection"]["state"], "isolated")
        checked = f.verify_unfinished(result)
        self.assertTrue(checked["failure_archive_verified"])
        self.assertFalse(checked["execution_completed"] or checked["phase_completed"] or checked["runtime_authority_granted"])

    def test_registered_wrapper_variants_and_local_pid_mismatch_stop(self):
        modes = ("isolation_capture_subclass", "isolation_capture_deep_cause",
                 "isolation_capture_context", "isolation_capture_pid")
        for mode in modes:
            with self.subTest(mode=mode):
                result = self.assert_stopped(mode)
                self.assertEqual(result["hits"].count(mode), 1)
                self.assertEqual(result["hits"].count("isolation_registered_original_verified"), 1)
                row = result["artifact"]["business"]["agent"]["IC-11"]
                self.assertEqual(row["case_rejection"]["state"], "rejected")
                self.assertFalse(row["case_rejection"]["provider_closed"])
                self.assertIsNone(row["case_rejection"]["sealed_event_hash"])
                if mode == "isolation_capture_pid":
                    self.assertGreaterEqual(result["hits"].count("isolation_local_pid_proxy_hit"), 1)
                    self.assertTrue(result["post_checks"]["isolation_os_reference_restored"])
                    self.assertEqual(result["artifact"]["authority"]["stopped"], "item6_isolation_factory_identity")

    def test_same_case_tool_and_next_case_before_seal_are_rejected(self):
        for mode in ("isolation_tool_after_reject", "isolation_open_before_sealed"):
            with self.subTest(mode=mode):
                result = self.assert_stopped(mode)
                self.assertEqual(result["hits"].count(mode), 1)
                row = result["artifact"]["business"]["agent"]["IC-11"]
                self.assertEqual(row["events"], [])
                self.assertIsNone(row["case_rejection"]["sealed_event_hash"])
                if mode == "isolation_tool_after_reject":
                    self.assertEqual(result["hits"].count("isolation_extra_tool_rejected"), 1)
                    self.assertEqual(result["post_checks"]["isolation_extra_tool_error"], "item6_isolation_case_work_denied")
                else:
                    self.assertEqual(result["hits"].count("isolation_next_case_open_rejected"), 1)
                    probe = result["post_checks"]["isolation_next_case_probe"]
                    self.assertEqual((probe["started_before"], probe["started_after"]), (0, 0))
                    self.assertEqual(probe["error_code"], "item6_isolation_pending")
                    self.assertIsNotNone(row["case_lifecycle"]["closed_event_hash"])
                    self.assertFalse(any(event["event_type"] == isolation.START
                        and event["safe_payload"]["task_id"] == probe["task_id"]
                        and event["safe_payload"]["path_kind"] == probe["path_kind"]
                        for event in result["artifact"]["audit"]["events"]))

    def test_actual_five_second_pending_transport_close_times_out(self):
        result = self.assert_stopped("isolation_provider_close_timeout")
        for hit in ("isolation_provider_close_timeout", "original_fake_delegate_close_returned",
                    "isolation_close_wait_cancelled"):
            self.assertEqual(result["hits"].count(hit), 1)
        observation = result["post_checks"]["isolation_cleanup_timeout"]
        self.assertEqual(observation["original_timeout_seconds"], 5)
        self.assertTrue(observation["awaited_real_pending_close"])
        self.assertTrue(observation["wait_cancelled"])
        self.assertTrue(observation["isolated_mock_delegate"])
        # A genuine awaited timeout, not a directly raised TimeoutError.
        self.assertGreaterEqual(observation["elapsed_seconds"], 4.0)
        row = result["artifact"]["business"]["agent"]["IC-11"]
        self.assertFalse(row["case_rejection"]["provider_closed"])
        self.assertIsNone(row["case_rejection"]["sealed_event_hash"])
        self.assertEqual(result["artifact"]["authority"]["stopped"],
                         "provider_completion_post_request_cleanup_failed")
