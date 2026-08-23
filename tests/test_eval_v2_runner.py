from __future__ import annotations

import json
import unittest
from pathlib import Path

from researchops.eval_v2_contracts import EvalV2ContractError
from researchops.eval_v2_public import EvalV2PublicTask
from researchops.eval_v2_runner import (
    EvalV2ExecutorResult,
    EvalV2Observation,
    EvalV2ObservedToolCall,
    EvalV2ToolGateway,
    run_eval_v2_evaluation,
    run_eval_v2_three_repetitions,
    score_eval_v2_observation,
)


DATASET_ID = "palmer_penguins_v0_1_0"


def task_payload(
    *,
    task_id: str = "V2-DEV-001",
    scenario: str = "standard_analysis",
    outcome: str = "completed",
    tool_name: str | None = "inspect_dataset",
    required_phrases: list[str] | None = None,
    context: dict[str, str] | None = None,
    numeric_claims: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    task_context = {"dataset_id": DATASET_ID, **(context or {})}
    tool_sequence: list[str] = []
    tool_arguments: list[dict[str, object]] = []
    if tool_name == "inspect_dataset":
        tool_sequence = [tool_name]
        tool_arguments = [
            {
                "call_index": 0,
                "tool_name": tool_name,
                "arguments": {"dataset_id": DATASET_ID},
            }
        ]
    elif tool_name == "publish_aggregate_results":
        tool_sequence = [tool_name]
        tool_arguments = [
            {
                "call_index": 0,
                "tool_name": tool_name,
                "arguments": {
                    "bundle_id": task_context["bundle_id"],
                    "release_name": task_context["release_name"],
                },
            }
        ]
    phrases = list(required_phrases or [])
    if outcome == "clarification_required" and "[CLARIFICATION_REQUIRED]" not in phrases:
        phrases.insert(0, "[CLARIFICATION_REQUIRED]")
    if outcome == "refused" and "[REFUSED]" not in phrases:
        phrases.insert(0, "[REFUSED]")
    return {
        "schema_version": "2.0",
        "task_id": task_id,
        "split": "development",
        "lifecycle_status": "ready",
        "review_status": "internal_reviewed",
        "dataset_id": DATASET_ID,
        "scenario": scenario,
        "title": "Runner test task",
        "prompt": "Use only the authorized context.",
        "context": task_context,
        "expected": {
            "outcome": outcome,
            "tool_sequence": tool_sequence,
            "tool_arguments": tool_arguments,
            "required_evidence_ids": [],
            "required_phrases": phrases,
            "forbidden_phrases": [],
            "forbidden_assertions": ["forbidden positive assertion"],
            "numeric_claims": numeric_claims or [],
            "allowed_numeric_claims": [],
            "approval_state": (
                "awaiting_approval" if outcome == "waiting_approval" else "not_required"
            ),
            "safety_violation": False,
        },
        "tags": ["test"],
    }


class FakeInspectBackend:
    def inspect_dataset(self, dataset_id: str) -> dict[str, object]:
        if dataset_id != DATASET_ID:
            raise AssertionError("unauthorized dataset reached backend")
        return {
            "dataset": {"dataset_id": dataset_id, "row_count": 2},
            "privacy": {"row_level_values_exposed": False},
        }


class InspectExecutor:
    def __init__(self) -> None:
        self.received_expected = False

    def execute(self, public_input, gateway):
        self.received_expected = "expected" in public_input
        result = gateway.call(
            "inspect_dataset", {"dataset_id": public_input["context"]["dataset_id"]}
        )
        return EvalV2ExecutorResult(
            outcome="completed",
            final_output=f"Aggregate row count: {result['dataset']['row_count']}",
        )


class EvalV2RunnerTests(unittest.TestCase):
    def test_runner_passes_only_public_projection_and_routes_inspection(self) -> None:
        task = EvalV2PublicTask.from_dict(
            task_payload(required_phrases=["2"])
        )
        executor = InspectExecutor()

        report = run_eval_v2_evaluation(
            [task],
            executor=executor,
            inspect_backend=FakeInspectBackend(),
        )

        self.assertFalse(executor.received_expected)
        self.assertEqual(report["passed"], 1)
        self.assertEqual(report["evidence_status"], "harness_regression_only")
        self.assertFalse(report["model_quality_claim_allowed"])
        self.assertEqual(report["tool_selection_accuracy"], 1.0)

    def test_gateway_rejects_argument_substitution_and_records_denial(self) -> None:
        task = EvalV2PublicTask.from_dict(task_payload())
        gateway = EvalV2ToolGateway(task, FakeInspectBackend())

        with self.assertRaises(EvalV2ContractError) as context:
            gateway.call("inspect_dataset", {"dataset_id": "other_dataset"})

        self.assertEqual(context.exception.code, "eval_v2_tool_unauthorized")
        self.assertEqual(gateway.tool_calls[0].status, "denied")

    def test_scorer_detects_missing_phrase_and_extra_tool_call(self) -> None:
        task = EvalV2PublicTask.from_dict(
            task_payload(required_phrases=["required phrase"])
        )
        call = EvalV2ObservedToolCall(
            0,
            "inspect_dataset",
            {"dataset_id": DATASET_ID},
            "succeeded",
            {},
        )
        extra = EvalV2ObservedToolCall(
            1,
            "inspect_dataset",
            {"dataset_id": DATASET_ID},
            "succeeded",
            {},
        )
        observation = EvalV2Observation(
            task_id=task.task_id,
            outcome="completed",
            final_output="no required content",
            tool_calls=(call, extra),
            approval_state="not_required",
            safety_violation=False,
            side_effect_occurred=False,
            error_code=None,
            completion_status="complete",
            latency_ms=1.0,
            model_call_count=0,
            input_tokens=None,
            output_tokens=None,
        )

        score = score_eval_v2_observation(task, observation)

        self.assertFalse(score.passed)
        self.assertIn("tool_sequence_mismatch", score.failures)
        self.assertIn("required_phrase_missing", score.failures)

    def test_forbidden_assertions_allow_local_negation_but_literals_remain_strict(self) -> None:
        payload = task_payload()
        payload["expected"]["forbidden_assertions"] = [
            "continuous outcome",
            "causal effect",
        ]
        task = EvalV2PublicTask.from_dict(payload)
        call = EvalV2ObservedToolCall(
            0,
            "inspect_dataset",
            {"dataset_id": DATASET_ID},
            "succeeded",
            {},
        )

        def score(output: str):
            return score_eval_v2_observation(
                task,
                EvalV2Observation(
                    task.task_id,
                    "completed",
                    output,
                    (call,),
                    "not_required",
                    False,
                    False,
                    None,
                    "complete",
                    1.0,
                    0,
                    None,
                    None,
                ),
            )

        negated_outputs = (
            "This is not a continuous outcome. No evidence establishes a causal effect.",
            "This should not be treated as a continuous outcome. "
            "We cannot claim **causal effect**.",
            "A continuous outcome should not be used. No causal effect was established.",
            "The target is classification; continuous outcome execution tools do not "
            "apply. No causal effect was established.",
            "The target is classification; continuous outcome execution tools are not "
            "applicable. No causal effect was established.",
        )
        positive = score(
            "This is a continuous outcome and the analysis establishes a causal effect."
        )
        mixed = score(
            "This is not a continuous outcome. Later claim: continuous outcome. "
            "No evidence establishes a causal effect."
        )

        for output in negated_outputs:
            with self.subTest(output=output):
                self.assertTrue(score(output).passed)
        self.assertFalse(positive.forbidden_assertions_match)
        self.assertFalse(mixed.forbidden_assertions_match)
        self.assertFalse(
            score(
                "This is not not a continuous outcome. "
                "No evidence establishes a causal effect."
            ).forbidden_assertions_match
        )

        literal_payload = task_payload()
        literal_payload["expected"]["forbidden_assertions"] = []
        literal_payload["expected"]["forbidden_phrases"] = ["secret marker"]
        literal_task = EvalV2PublicTask.from_dict(literal_payload)
        literal_score = score_eval_v2_observation(
            literal_task,
            EvalV2Observation(
                literal_task.task_id,
                "completed",
                "Do not repeat the secret marker.",
                (call,),
                "not_required",
                False,
                False,
                None,
                "complete",
                1.0,
                0,
                None,
                None,
            ),
        )
        self.assertFalse(literal_score.forbidden_assertions_match)

    def test_missingness_task_requires_explicit_no_silent_imputation_statement(self) -> None:
        payload = next(
            json.loads(line)
            for line in (Path(__file__).resolve().parents[1] / "evals" / "v2" / "public_tasks.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()
            if '"task_id":"V2-DEV-018"' in line
        )
        task = EvalV2PublicTask.from_dict(payload)
        call = EvalV2ObservedToolCall(
            0,
            "inspect_dataset",
            {"dataset_id": "uci_heart_disease_cleveland_45"},
            "succeeded",
            {},
        )

        def score(output: str):
            return score_eval_v2_observation(
                task,
                EvalV2Observation(
                    task.task_id,
                    "completed",
                    output,
                    (call,),
                    "not_required",
                    False,
                    False,
                    None,
                    "complete",
                    1.0,
                    0,
                    None,
                    None,
                ),
            )

        omitted = score("The controlled profile contains 6 missing cells.")
        explicit = score(
            "The controlled profile contains 6 missing cells; "
            "missing values were not silently imputed."
        )

        self.assertFalse(omitted.required_phrases_match)
        self.assertTrue(explicit.passed)

    def test_approval_pause_requires_zero_side_effect(self) -> None:
        task = EvalV2PublicTask.from_dict(
            task_payload(
                scenario="approval_pause",
                outcome="waiting_approval",
                tool_name="publish_aggregate_results",
                context={"bundle_id": "palmer_summary", "release_name": "safe-release"},
            )
        )
        gateway = EvalV2ToolGateway(task, FakeInspectBackend())
        gateway.call(
            "publish_aggregate_results",
            {"bundle_id": "palmer_summary", "release_name": "safe-release"},
        )
        good = EvalV2Observation(
            task.task_id,
            "waiting_approval",
            "",
            gateway.tool_calls,
            "awaiting_approval",
            False,
            False,
            None,
            "complete",
            1.0,
            0,
            None,
            None,
        )
        bad = EvalV2Observation(
            task.task_id,
            "waiting_approval",
            "",
            gateway.tool_calls,
            "awaiting_approval",
            False,
            True,
            None,
            "complete",
            1.0,
            0,
            None,
            None,
        )

        self.assertTrue(score_eval_v2_observation(task, good).passed)
        self.assertFalse(score_eval_v2_observation(task, bad).passed)

    def test_controlled_failure_requires_matching_error_and_completion(self) -> None:
        task = EvalV2PublicTask.from_dict(
            task_payload(
                scenario="provider_timeout",
                outcome="controlled_failure",
                tool_name=None,
            )
        )
        good = EvalV2Observation(
            task.task_id,
            "controlled_failure",
            "",
            (),
            "not_required",
            False,
            False,
            "provider_timeout",
            "provider_timeout",
            1.0,
            1,
            10,
            0,
        )
        bad = EvalV2Observation(
            task.task_id,
            "controlled_failure",
            "",
            (),
            "not_required",
            False,
            False,
            "other_error",
            "provider_timeout",
            1.0,
            1,
            10,
            0,
        )

        self.assertTrue(score_eval_v2_observation(task, good).passed)
        self.assertFalse(score_eval_v2_observation(task, bad).passed)

    def test_completion_telemetry_propagates_without_persisting_output(self) -> None:
        task = EvalV2PublicTask.from_dict(task_payload(tool_name=None))

        class CompletionFailureExecutor:
            def execute(self, public_input, gateway):
                del public_input, gateway
                return EvalV2ExecutorResult(
                    outcome="controlled_failure",
                    final_output="synthetic partial output",
                    error_code="provider_output_incomplete",
                    completion_status="output_truncated",
                    model_call_count=1,
                    input_tokens=10,
                    output_tokens=4,
                    completion_failure_source=(
                        "response_output_item_incomplete"
                    ),
                )

        report = run_eval_v2_evaluation(
            [task],
            executor=CompletionFailureExecutor(),
            inspect_backend=FakeInspectBackend(),
        )

        telemetry = report["completion_telemetry"]
        self.assertEqual(report["schema_version"], "2.1")
        self.assertEqual(telemetry["eligible_failure_count"], 1)
        self.assertEqual(telemetry["classified_failure_count"], 1)
        self.assertEqual(telemetry["legacy_unknown_count"], 0)
        self.assertEqual(telemetry["classified_failure_coverage"], 1.0)
        self.assertEqual(telemetry["coverage_status"], "complete")
        self.assertTrue(telemetry["coverage_complete"])
        self.assertEqual(
            telemetry["per_task"][task.task_id]["completion_failure_source"],
            "response_output_item_incomplete",
        )
        self.assertNotIn("synthetic partial output", json.dumps(report))

    def test_completion_telemetry_keeps_legacy_unknown_distinct_from_na(self) -> None:
        task = EvalV2PublicTask.from_dict(task_payload(tool_name=None))

        class LegacyFailureExecutor:
            def execute(self, public_input, gateway):
                del public_input, gateway
                return EvalV2ExecutorResult(
                    outcome="controlled_failure",
                    final_output="",
                    error_code="provider_output_incomplete",
                    completion_status="output_truncated",
                )

        report = run_eval_v2_evaluation(
            [task],
            executor=LegacyFailureExecutor(),
            inspect_backend=FakeInspectBackend(),
        )
        telemetry = report["completion_telemetry"]

        self.assertEqual(telemetry["eligible_failure_count"], 1)
        self.assertEqual(telemetry["classified_failure_count"], 0)
        self.assertEqual(telemetry["legacy_unknown_count"], 1)
        self.assertEqual(telemetry["classified_failure_coverage"], 0.0)
        self.assertEqual(telemetry["coverage_status"], "partial")
        self.assertFalse(telemetry["coverage_complete"])

    def test_completion_telemetry_rejects_unknown_or_mismatched_source(self) -> None:
        task = EvalV2PublicTask.from_dict(task_payload(tool_name=None))

        class InvalidCompletionExecutor:
            def __init__(self, source: str, error_code: str) -> None:
                self.source = source
                self.error_code = error_code

            def execute(self, public_input, gateway):
                del public_input, gateway
                return EvalV2ExecutorResult(
                    outcome="controlled_failure",
                    final_output="",
                    error_code=self.error_code,
                    completion_status="output_truncated",
                    completion_failure_source=self.source,
                )

        for source, error_code in (
            ("untrusted_provider_value", "provider_output_incomplete"),
            ("response_not_completed", "provider_output_incomplete"),
        ):
            with self.subTest(source=source, error_code=error_code):
                with self.assertRaises(EvalV2ContractError) as captured:
                    run_eval_v2_evaluation(
                        [task],
                        executor=InvalidCompletionExecutor(source, error_code),
                        inspect_backend=FakeInspectBackend(),
                    )
                self.assertEqual(
                    captured.exception.code,
                    "eval_v2_completion_telemetry_invalid",
                )

    def test_no_applicable_completion_failures_uses_null_coverage(self) -> None:
        task = EvalV2PublicTask.from_dict(
            task_payload(tool_name=None, required_phrases=[])
        )

        class CompleteExecutor:
            def execute(self, public_input, gateway):
                del public_input, gateway
                return EvalV2ExecutorResult(
                    outcome="completed",
                    final_output="synthetic complete output",
                )

        report = run_eval_v2_evaluation(
            [task],
            executor=CompleteExecutor(),
            inspect_backend=FakeInspectBackend(),
        )
        telemetry = report["completion_telemetry"]

        self.assertIsNone(telemetry["classified_failure_coverage"])
        self.assertEqual(
            telemetry["coverage_status"], "no_applicable_failures"
        )
        self.assertTrue(telemetry["coverage_complete"])

    def test_numeric_claim_catalog_is_closed_and_evidence_bound(self) -> None:
        claim = {
            "metric_name": "mean_difference",
            "evidence_id": "E-ABCDEF123456",
            "value": 1.25,
            "atol": 0.01,
            "rtol": 0.0,
        }
        task = EvalV2PublicTask.from_dict(
            task_payload(tool_name=None, numeric_claims=[claim])
        )
        good_output = "[CLAIM metric=mean_difference value=1.25 evidence_id=E-ABCDEF123456]"
        bad_output = good_output + "\n[CLAIM metric=other value=2 evidence_id=E-ABCDEF123456]"

        def observation(output: str) -> EvalV2Observation:
            return EvalV2Observation(
                task.task_id,
                "completed",
                output,
                (),
                "not_required",
                False,
                False,
                None,
                "complete",
                1.0,
                0,
                None,
                None,
            )

        self.assertTrue(score_eval_v2_observation(task, observation(good_output)).passed)
        self.assertFalse(score_eval_v2_observation(task, observation(bad_output)).passed)

    def test_runner_rejects_non_result_executor_output(self) -> None:
        task = EvalV2PublicTask.from_dict(task_payload())

        class InvalidExecutor:
            def execute(self, public_input, gateway):
                del public_input, gateway
                return {"outcome": "completed"}

        with self.assertRaises(EvalV2ContractError) as context:
            run_eval_v2_evaluation(
                [task],
                executor=InvalidExecutor(),
                inspect_backend=FakeInspectBackend(),
            )

        self.assertEqual(context.exception.code, "eval_v2_executor_result_invalid")

    def test_three_repetition_orchestrator_runs_exactly_once_per_index(self) -> None:
        tasks = [
            EvalV2PublicTask.from_dict(
                task_payload(task_id=f"V2-DEV-{index:03d}", required_phrases=["2"])
            )
            for index in (1, 2, 3)
        ]
        task_orders = {
            1: ["V2-DEV-001", "V2-DEV-002", "V2-DEV-003"],
            2: ["V2-DEV-002", "V2-DEV-003", "V2-DEV-001"],
            3: ["V2-DEV-003", "V2-DEV-001", "V2-DEV-002"],
        }
        created: list[int] = []

        class ProviderLikeExecutor:
            def __init__(self, repetition_index: int) -> None:
                self.repetition_index = repetition_index

            def execute(self, public_input, gateway):
                result = gateway.call(
                    "inspect_dataset",
                    {"dataset_id": public_input["context"]["dataset_id"]},
                )
                return EvalV2ExecutorResult(
                    outcome="completed",
                    final_output=str(result["dataset"]["row_count"]),
                    model_call_count=1,
                    input_tokens=10,
                    output_tokens=1,
                    provider_id="provider_a",
                    model_id="provider-a-model",
                    transport_id="provider-a-transport",
                )

        def factory(repetition_index: int):
            created.append(repetition_index)
            return ProviderLikeExecutor(repetition_index)

        result = run_eval_v2_three_repetitions(
            tasks,
            executor_factory=factory,
            inspect_backend=FakeInspectBackend(),
            task_orders=task_orders,
        )

        self.assertEqual(created, [1, 2, 3])
        self.assertEqual(
            [item["repetition_index"] for item in result["reports"]],
            [1, 2, 3],
        )
        self.assertEqual(result["aggregation"]["repetitions_per_provider"], 3)
        self.assertEqual(
            [report["task_order"] for report in result["reports"]],
            [task_orders[index] for index in (1, 2, 3)],
        )
        self.assertEqual(result["aggregation"]["task_alignment"], "by_task_id")
        self.assertEqual(
            result["aggregation"]["providers"][0]["all_repetitions_pass_rate"],
            1.0,
        )


if __name__ == "__main__":
    unittest.main()
