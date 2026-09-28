import unittest
from copy import deepcopy
from types import SimpleNamespace
from . import item6_experiment_fixture as f


class _UnitOutputItem(SimpleNamespace):
    def model_dump(self, **kwargs):
        def plain(value):
            if isinstance(value, SimpleNamespace):
                return {name: plain(item) for name, item in vars(value).items()}
            if isinstance(value, list): return [plain(item) for item in value]
            return deepcopy(value)
        return plain(self)


class ObservedModelMixedUnitTests(unittest.IsolatedAsyncioTestCase):
    """Synthetic parser units only; no entrypoint, Provider, owner, claim or store evidence."""

    @staticmethod
    def tool(**changes):
        value = dict(type="function_call", id="fc_unit", call_id="call_unit",
                     name="read_aggregate", arguments='{"bundle_id":"aggregate-01"}', status="completed")
        value.update(changes)
        return value

    @staticmethod
    def message(text="伴随文字。", **changes):
        value = dict(type="message", id="msg_unit", role="assistant", status="completed",
                     content=[dict(type="output_text", text=text, annotations=[])])
        value.update(changes)
        return value

    def context(self, *outputs):
        from researchops_item6_experiment_v1.runner import _ObservedModel
        responses = []
        for output in outputs:
            items = []
            for original in output:
                value = deepcopy(original)
                if value.get("type") == "message":
                    value["content"] = [SimpleNamespace(**part) for part in value["content"]]
                items.append(_UnitOutputItem(**value))
            responses.append(SimpleNamespace(output=items,
                usage=SimpleNamespace(request_usage_entries=[{}], requests=1)))
        pending = list(responses)
        class Delegate:
            async def get_response(self, *args, **kwargs): return pending.pop(0)
        case = SimpleNamespace(text=None, status="unknown", completion="unknown",
            replay=[{"role": "user", "content": "synthetic unit input"}],
            sdk_raw_count=0, sdk_requests=0, sdk_usage_indices={}, sdk_observation_complete=True,
            record={"native_call_ids": [], "plan": []},
            factory=SimpleNamespace(canary="offline-unit-sensitive-canary"))
        def plan(tool, arguments, source):
            case.record["plan"].append(dict(tool=tool, arguments=deepcopy(arguments), source=source))
        case.plan = plan
        return _ObservedModel(Delegate(), case), case, responses

    async def reject(self, output, code=None, *, prior_ids=()):
        from researchops_item6_experiment_v1 import contract as c
        model, case, _ = self.context(output)
        case.record["native_call_ids"] = list(prior_ids)
        original_replay = deepcopy(case.replay)
        assertion = self.assertRaisesRegex(c.ExperimentError, "^item6_" + code + "$") if code else self.assertRaises(c.ExperimentError)
        with assertion: await model.get_response()
        self.assertIsNone(case.text)
        self.assertEqual(case.completion, "unknown")
        self.assertEqual(case.replay, original_replay)

    async def test_mixed_orders_preserve_raw_response_replay_and_wait_for_final(self):
        final_text = "  最终合成答案。\n"
        for before in (True, False):
            for companion in ("先读取合成结果。", "", " \t\n　"):
                with self.subTest(message_before=before, companion=repr(companion)):
                    output = [self.message(companion), self.tool()] if before else [self.tool(), self.message(companion)]
                    final = [self.message(final_text, id="msg_final")]
                    model, case, responses = self.context(output, final)
                    original_replay = deepcopy(case.replay)
                    self.assertIs(await model.get_response(), responses[0])
                    self.assertEqual([item.model_dump() for item in responses[0].output], output)
                    self.assertEqual(case.replay, original_replay + output)
                    self.assertIsNone(case.text)
                    self.assertEqual((case.status, case.completion), ("unknown", "unknown"))
                    self.assertEqual(case.record["native_call_ids"], ["call_unit"])
                    self.assertEqual(len(case.record["plan"]), 1)
                    self.assertIs(await model.get_response(), responses[1])
                    self.assertEqual(case.text, final_text)
                    self.assertEqual((case.status, case.completion), ("completed", "complete"))
                    self.assertEqual(case.replay, original_replay + output + final)
                    self.assertEqual(case.sdk_raw_count, 2)
                    self.assertEqual(case.sdk_requests, 2)

    async def test_pure_final_empty_and_whitespace_text_remain_exact(self):
        for text in ("", " \t\n　", "原始最终文字。"):
            with self.subTest(text=repr(text)):
                output = [self.message(text)]
                model, case, responses = self.context(output)
                self.assertIs(await model.get_response(), responses[0])
                self.assertEqual(case.text, text)
                self.assertEqual(case.completion, "complete")
                self.assertEqual(case.replay[-1], output[0])

    async def test_single_empty_content_companion_still_waits_for_final(self):
        for before in (True, False):
            with self.subTest(message_before=before):
                empty = self.message(content=[])
                output = [empty, self.tool()] if before else [self.tool(), empty]
                model, case, responses = self.context(output)
                original_replay = deepcopy(case.replay)
                self.assertIs(await model.get_response(), responses[0])
                self.assertEqual(case.replay, original_replay + output)
                self.assertIsNone(case.text)
                self.assertEqual((case.status, case.completion), ("unknown", "unknown"))
                self.assertEqual(case.record["native_call_ids"], ["call_unit"])
                self.assertEqual(len(case.record["plan"]), 1)

    async def test_incomplete_missing_and_null_tool_status_still_reject(self):
        for status in ("incomplete", None, "missing"):
            with self.subTest(status=status):
                tool = self.tool(status=status)
                if status == "missing": tool.pop("status")
                await self.reject([self.message(), tool], "function_call_not_complete")

    async def test_duplicate_invalid_call_ids_tools_and_arguments_still_reject(self):
        await self.reject([self.tool(), self.tool()], "call_identity")
        await self.reject([self.message(), self.tool()], "call_identity", prior_ids=("call_unit",))
        for changes, code in (({"call_id": None}, "call_identity"), ({"call_id": 7}, "call_identity"),
                              ({"name": "publish_results"}, "model_tool"), ({"arguments": {}}, "model_tool"),
                              ({"arguments": '{"wrong":"aggregate-01"}'}, "model_arguments"),
                              ({"arguments": "{broken"}, "json")):
            with self.subTest(changes=changes):
                await self.reject([self.message(), self.tool(**changes)], code)

    async def test_multiple_calls_messages_parts_and_empty_actions_still_reject(self):
        two_parts = self.message(content=[dict(type="output_text", text="一", annotations=[]),
                                          dict(type="output_text", text="二", annotations=[])])
        cases = {
            "distinct_calls": [self.tool(), self.tool(id="fc_second", call_id="call_second")],
            "two_messages": [self.message(), self.message(id="msg_second")],
            "two_empty_messages_with_tool": [self.message(content=[]), self.tool(), self.message(id="msg_second", content=[])],
            "two_parts": [two_parts],
            "mixed_two_parts": [self.tool(), two_parts],
            "empty_output": [],
            "empty_message": [self.message(content=[])],
        }
        for name, output in cases.items():
            with self.subTest(shape=name): await self.reject(output, "response_actions")

    async def test_unknown_reasoning_and_invalid_message_fields_still_reject(self):
        cases = (([{"type": "future_output"}], "output_item"),
                 ([{"type": "reasoning"}], "reasoning_not_allowed"),
                 ([self.tool(), self.message(status="incomplete")], "message_state"),
                 ([self.tool(), self.message(status=None)], "message_state"),
                 ([self.tool(), self.message(role="user")], "message_state"),
                 ([self.tool(), self.message(None)], "text_part"),
                 ([self.message(None)], "text_part"),
                 ([self.tool(), self.message(content=[dict(type="refusal", text="拒绝")])], "text_part"))
        for output, code in cases:
            with self.subTest(output=output): await self.reject(output, code)

    async def test_companion_privacy_scan_remains_active_in_both_orders(self):
        from researchops_external_closure.errors import ExternalClosurePrimitiveError
        for before in (True, False):
            for text in ("person@example.invalid", "offline-unit-sensitive-canary"):
                with self.subTest(message_before=before, privacy_case=text):
                    output = [self.message(text), self.tool()] if before else [self.tool(), self.message(text)]
                    model, case, _ = self.context(output)
                    original_replay = deepcopy(case.replay)
                    with self.assertRaisesRegex(ExternalClosurePrimitiveError, "external_closure_sensitive_content_detected"):
                        await model.get_response()
                    self.assertIsNone(case.text)
                    self.assertEqual(case.replay, original_replay)


class SafeErrorUnitTests(unittest.TestCase):
    """Error attribution only: these objects confer no runtime authority."""

    def setUp(self):
        from agents.exceptions import UserError
        from researchops_item6_experiment_v1 import contract
        self.contract, self.user_error = contract, UserError
        self.code = "item6_tool_before_design_or_refusal"
        self.generic = "item6_execution_failed"

    def wrapped(self, cause=None):
        error = self.user_error("synthetic wrapper")
        error.__cause__ = cause
        return error

    def inner(self):
        return self.contract.ExperimentError("tool_before_design_or_refusal")

    def test_direct_project_codes_keep_existing_behavior(self):
        for code in ("tool_before_design_or_refusal", "source_manifest_drift", "owner_stopped"):
            with self.subTest(code=code):
                self.assertEqual(self.contract.safe_error(self.contract.ExperimentError(code)), "item6_" + code)

    def test_exact_sdk_wrapper_preserves_confirmed_refusal_code(self):
        self.assertEqual(self.contract.safe_error(self.wrapped(self.inner())), self.code)

    def test_other_wrapped_project_codes_remain_generic(self):
        for code in ("source_manifest_drift", "owner_stopped", "future_code"):
            with self.subTest(code=code):
                self.assertEqual(self.contract.safe_error(self.wrapped(self.contract.ExperimentError(code))), self.generic)

    def test_no_cause_or_context_only_remains_generic(self):
        error = self.wrapped()
        self.assertEqual(self.contract.safe_error(error), self.generic)
        error.__context__ = self.inner()
        self.assertEqual(self.contract.safe_error(error), self.generic)

    def test_nested_and_cyclic_wrappers_are_not_walked(self):
        nested = self.wrapped(self.wrapped(self.inner()))
        cyclic = self.wrapped(); cyclic.__cause__ = cyclic
        for error in (nested, cyclic):
            self.assertEqual(self.contract.safe_error(error), self.generic)

    def test_non_sdk_wrappers_and_exception_groups_remain_generic(self):
        error = ValueError("synthetic wrapper"); error.__cause__ = self.inner()
        group = ExceptionGroup("synthetic group", [self.inner()])
        for value in (error, group):
            self.assertEqual(self.contract.safe_error(value), self.generic)

    def test_sdk_subclass_and_same_named_impostor_are_not_unwrapped(self):
        subclass = type("UserError", (self.user_error,), {"__module__": "agents.exceptions"})
        impostor = type("UserError", (ValueError,), {"__module__": "agents.exceptions"})
        for cls in (subclass, impostor):
            value = cls("synthetic wrapper"); value.__cause__ = self.inner()
            self.assertEqual(self.contract.safe_error(value), self.generic)

    def test_cause_subclass_and_module_impostor_are_not_trusted(self):
        actual = self.contract.ExperimentError
        subclass = type("ExperimentError", (actual,), {"__module__": actual.__module__})
        impostor = type("ExperimentError", (ValueError,), {"__module__": actual.__module__})
        for cls in (subclass, impostor):
            value = cls("tool_before_design_or_refusal"); value.code = self.code
            self.assertEqual(self.contract.safe_error(self.wrapped(value)), self.generic)

    def test_only_exact_string_code_is_allowed(self):
        class StringSubclass(str): pass
        for code in (None, 1, False, [self.code], StringSubclass(self.code), self.code + "\n"):
            inner = self.inner(); inner.code = code
            self.assertEqual(self.contract.safe_error(self.wrapped(inner)), self.generic)

    def test_code_in_message_or_outer_attribute_does_not_grant_attribution(self):
        error = self.user_error(self.code); error.code = self.code
        self.assertEqual(self.contract.safe_error(error), self.generic)
        inner = self.inner(); del inner.code
        self.assertEqual(self.contract.safe_error(self.wrapped(inner)), self.generic)

    def test_payload_fields_are_not_read_or_formatted(self):
        class Unread:
            def __str__(self): raise AssertionError("payload must not be formatted")
            def __repr__(self): raise AssertionError("payload must not be formatted")
        payload = Unread()
        inner = self.inner(); inner.args = (payload,)
        error = self.wrapped(inner)
        error.message = payload; error.args = (payload,); error.run_data = payload
        self.assertEqual(self.contract.safe_error(error), self.code)
        self.assertIs(error.__cause__, inner)
        self.assertIs(error.run_data, payload)

    def test_sensitive_text_is_never_returned_and_exceptions_are_not_mutated(self):
        canary = "sk-synthetic-offline-canary-never-a-real-key"
        inner = self.inner(); inner.args = (canary,)
        error = self.user_error("Authorization: " + canary)
        error.__cause__ = inner
        original_args = error.args
        self.assertEqual(self.contract.safe_error(error), self.code)
        self.assertEqual(inner.args, (canary,))
        self.assertIs(error.args, original_args)
        self.assertIs(error.__cause__, inner)


class SessionTests(unittest.TestCase):
    def test_ic11_design_refusal_survives_actual_sdk_and_failed_archive_readback(self):
        result = f.run_case("ic11_tool_before_design")
        self.assertEqual(result["process_exit_code"], 2, result.get("error"))
        self.assertEqual(result["claim_files"], 1)
        self.assertEqual(result["hits"].count("ic11_tool_before_design_response_injected"), 1)
        self.assertEqual([row["task_id"] for row in result["calls"]],
                         ["IC-01", "IC-01", "IC-07", "IC-07", "IC-07", "IC-11"])
        self.assertEqual((result["provider_calls"], result["network_attempts"]), (0, 0))
        self.assertFalse(result["real_store_touched"])
        artifact = result["artifact"]
        code = "item6_tool_before_design_or_refusal"
        self.assertEqual((artifact["status"], artifact["authority"]["stopped"]), ("failed", code))
        row = artifact["business"]["agent"]["IC-11"]
        self.assertEqual(row["task_input"]["design_requests"], [])
        self.assertEqual((row["execution_state"], row["status"]), ("observed", "failed"))
        self.assertEqual(row["known_failures"], [{"code": code}])
        self.assertIsNone(row["final_output"])
        self.assertEqual(row["events"], [])
        self.assertEqual(row["allowed_evidence"], [])
        self.assertEqual(len(row["plan"]), 1)
        self.assertEqual(row["plan"][0]["execution"], "not_executed")
        self.assertEqual(artifact["business"]["fixed_workflow"]["IC-11"]["status"], "clarification")
        all_rows = [value for path in artifact["business"].values() for value in path.values()]
        self.assertEqual(len(all_rows), 32)
        self.assertEqual(sum(value["execution_state"] == "not_executed" for value in all_rows), 26)
        self.assertEqual((len(artifact["budget"]["requests"]), len(artifact["segments"])), (6, 6))
        checked = f.verify_result(result)
        self.assertEqual(checked["actual_exit_code"], 0, checked)
        self.assertTrue(checked["archive_verified"])
        self.assertFalse(checked["execution_completed"])
        self.assertFalse(checked["runtime_authority_granted"])
        self.assertEqual(checked["business_denominator"], 32)

    def test_mixed_before_actual_entry_replay_audit_and_independent_readback(self):
        # The opposite order is covered above as a unit, not claimed as an entrypoint run.
        result = f.run_case("mixed_before")
        self.assertEqual(result["process_exit_code"], 0, result.get("error"))
        self.assertEqual(result["claim_files"], 1)
        self.assertEqual((result["provider_calls"], result["network_attempts"]), (0, 0))
        self.assertFalse(result["real_store_touched"])
        self.assertTrue(result["synthetic_approval"] and result["synthetic_commit"])
        self.assertIn("mixed_response_injected", result["hits"])
        self.assertIn("mixed_followup_request_observed", result["hits"])
        wire = result["mixed_response_observation"]
        self.assertEqual(wire["origin"], "synthetic_mocktransport_only")
        companion, call = wire["injected_output"]
        self.assertEqual([companion["type"], call["type"]], ["message", "function_call"])
        replay = [item for item in wire["followup_input"] if item.get("type") in {"message", "function_call", "function_call_output"}]
        self.assertEqual([item["type"] for item in replay], ["message", "function_call", "function_call_output"])
        self.assertEqual(replay[0]["content"], companion["content"])
        self.assertEqual(replay[1]["arguments"], call["arguments"])
        self.assertEqual(replay[1]["call_id"], call["call_id"])
        self.assertEqual(replay[2]["call_id"], call["call_id"])
        artifact = result["artifact"]
        self.assertEqual((artifact["mode"], artifact["dispatches"]), ("offline_test", 30))
        for path in ("agent", "fixed_workflow"):
            self.assertEqual(len(artifact["business"][path]), 16)
            self.assertEqual(artifact["scores"][path]["raw_report"]["counts"]["planned"], 16)
            self.assertEqual(artifact["scores"][path]["raw_report"]["counts"]["pass"], 16)
        row = artifact["business"]["agent"]["IC-01"]
        self.assertEqual(row["native_call_ids"], [call["call_id"]])
        self.assertEqual(row["plan"][0]["execution"], "executed")
        self.assertEqual(len(row["events"]), 1)
        self.assertEqual(row["final_output"], "Spruce的质量为43.20 mg [E1]。")
        self.assertNotEqual(row["final_output"], companion["content"][0]["text"])
        self.assertEqual(len(artifact["budget"]["requests"]), 30)
        for request in artifact["budget"]["requests"]:
            self.assertTrue(request["cost_settled"])
            self.assertEqual((request["usage"]["input_tokens"], request["usage"]["output_tokens"]), (100, 30))
        terminal = [event for event in artifact["audit"]["events"] if event["event_type"] == "model_response_telemetry_recorded"]
        self.assertEqual((len(terminal), len(artifact["segments"])), (30, 30))
        for event, segment in zip(terminal, artifact["segments"]):
            self.assertEqual(event["safe_payload"]["attempt_index"], segment["attempt_index"])
            self.assertEqual(event["safe_payload"]["timing"]["segment_commitment_sha256"], segment["segment_commitment_sha256"])
        checked = f.verify_result(result)
        self.assertEqual(checked["actual_exit_code"], 0, checked)
        self.assertTrue(checked["archive_verified"])
        self.assertFalse(checked["runtime_authority_granted"])
        self.assertEqual(checked["business_denominator"], 32)

    def test_mixed_tool_audit_failure_does_not_publish_companion_as_final(self):
        result = f.run_case("mixed_tool_audit_failure")
        self.assertEqual(result["process_exit_code"], 2, result.get("error"))
        self.assertIn("mixed_response_injected", result["hits"])
        self.assertIn("mixed_tool_finish_audit_failure", result["hits"])
        self.assertEqual(result["hits"].count("agent_tool_read_observed"), 1)
        self.assertEqual(len(result["calls"]), 1)
        self.assertEqual((result["provider_calls"], result["network_attempts"]), (0, 0))
        self.assertFalse(result["real_store_touched"])
        self.assertIsNone(result["mixed_response_observation"]["followup_input"])
        artifact = result["artifact"]
        self.assertEqual(artifact["authority"]["stopped"], "item6_tool_finish_audit_failed")
        self.assertEqual(artifact["status"], "failed")
        row = artifact["business"]["agent"]["IC-01"]
        self.assertIsNone(row["final_output"])
        self.assertEqual(row["text_observation"], "unobserved")
        self.assertEqual(row["events"][0]["status"], "failed")
        self.assertEqual(row["events"][0]["produced_artifacts"], [])
        self.assertEqual(row["allowed_evidence"], [])
        for path in ("agent", "fixed_workflow"):
            self.assertEqual(len(artifact["business"][path]), 16)

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
