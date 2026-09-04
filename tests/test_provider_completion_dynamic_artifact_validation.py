"""Disk revalidation tests for the runtime denominator subartifact only."""

from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path

from researchops_completion_telemetry.capture import (
    RuntimeDenominatorTracker,
    VerifiedRuntimeDenominatorPlanBinding,
)
from researchops_completion_telemetry.sanitization import (
    CompletionTelemetryError,
    RUNTIME_DENOMINATOR_ARTIFACT_SCHEMA_VERSION,
    validate_completion_artifact,
    validate_runtime_denominator_artifact,
)
from tests.test_provider_completion_dynamic_denominator import _capture, _plan_binding


def _accepted_artifact() -> tuple[VerifiedRuntimeDenominatorPlanBinding, dict]:
    plan = _plan_binding(
        case_ids=("CASE-001", "CASE-002"),
        max_turns=2,
        request_cap=4,
    )
    tracker = RuntimeDenominatorTracker(plan)
    for case_id in plan.case_ids:
        handle = tracker.begin_attempt(case_id)
        tracker.finalize_response_accepted(handle, _capture())
        tracker.seal_case(
            case_id,
            sdk_raw_response_count=1,
            sdk_usage_request_count=1,
            sdk_request_usage_indices_by_response={0: (0,)},
        )
    # JSON round-trip is the supported disk-read boundary.
    artifact = json.loads(json.dumps(tracker.seal_runtime().to_dict()))
    return plan, artifact


def _multi_response_artifact() -> tuple[
    VerifiedRuntimeDenominatorPlanBinding, dict
]:
    plan = _plan_binding(
        case_ids=("CASE-001",),
        max_turns=2,
        request_cap=2,
    )
    tracker = RuntimeDenominatorTracker(plan)
    for _ in range(2):
        handle = tracker.begin_attempt("CASE-001")
        tracker.finalize_response_accepted(handle, _capture())
    tracker.seal_case(
        "CASE-001",
        sdk_raw_response_count=2,
        sdk_usage_request_count=2,
        # A raw response still has a row when it has no per-response usage
        # entries; only the nested index list is empty.
        sdk_request_usage_indices_by_response={0: (), 1: (0,)},
    )
    return plan, json.loads(json.dumps(tracker.seal_runtime().to_dict()))


class ProviderCompletionDynamicDenominatorSubartifactValidationTests(
    unittest.TestCase
):
    def assertInvalid(
        self,
        artifact: object,
        plan: object,
        expected_code: str,
    ) -> None:
        with self.assertRaises(CompletionTelemetryError) as caught:
            validate_runtime_denominator_artifact(
                artifact,  # type: ignore[arg-type]
                plan_binding=plan,
            )
        self.assertEqual(caught.exception.code, expected_code)

    def test_disk_round_trip_denominator_subartifact_validates(self) -> None:
        plan, artifact = _accepted_artifact()
        self.assertEqual(
            artifact["schema_version"],
            RUNTIME_DENOMINATOR_ARTIFACT_SCHEMA_VERSION,
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "runtime-denominator.json"
            path.write_text(
                json.dumps(artifact, ensure_ascii=False, sort_keys=True),
                encoding="utf-8",
            )
            read_back = json.loads(path.read_text(encoding="utf-8"))
        validate_runtime_denominator_artifact(read_back, plan_binding=plan)
        # The compatibility public name must dispatch to the same dynamic path.
        validate_completion_artifact(read_back, plan_binding=plan)

    def test_rejected_denominator_subartifact_needs_no_live_record(self) -> None:
        plan = _plan_binding(
            case_ids=("CASE-001",),
            max_turns=1,
            request_cap=1,
        )
        tracker = RuntimeDenominatorTracker(plan)
        handle = tracker.begin_attempt("CASE-001")
        tracker.finalize_response_rejected(
            handle, "provider_completion_capture_failed"
        )
        tracker.seal_case(
            "CASE-001",
            sdk_raw_response_count=1,
            sdk_usage_request_count=1,
            sdk_request_usage_indices_by_response={0: (0,)},
        )
        artifact = json.loads(json.dumps(tracker.seal_runtime().to_dict()))
        self.assertEqual(artifact["observed_response_count"], 1)
        self.assertEqual(artifact["records"], [])
        validate_runtime_denominator_artifact(artifact, plan_binding=plan)

    def test_denominator_subartifact_tampering_fails_closed(
        self,
    ) -> None:
        plan, original = _accepted_artifact()
        cases = (
            (
                "extra_field",
                lambda value: value.__setitem__("extra", None),
                "completion_telemetry_runtime_artifact_shape_invalid",
            ),
            (
                "plan_commitment",
                lambda value: value.__setitem__(
                    "preregistration_commitment", "0" * 64
                ),
                "completion_telemetry_runtime_artifact_plan_binding_mismatch",
            ),
            (
                "count",
                lambda value: value.__setitem__("observed_response_count", 1),
                "completion_telemetry_runtime_artifact_count_mismatch",
            ),
            (
                "terminal_count",
                lambda value: value["terminal_kind_counts"].__setitem__(
                    "response_accepted", 1
                ),
                "completion_telemetry_runtime_artifact_terminal_counts_invalid",
            ),
            (
                "attempt_index",
                lambda value: value["attempts"][1].__setitem__(
                    "attempt_index", 7
                ),
                "completion_telemetry_runtime_artifact_attempt_invalid",
            ),
            (
                "attempt_extra_field",
                lambda value: value["attempts"][0].__setitem__(
                    "provider_body", "forbidden"
                ),
                "completion_telemetry_runtime_artifact_attempt_invalid",
            ),
            (
                "case_count",
                lambda value: value["cases"][0].__setitem__(
                    "attempts_terminal", 0
                ),
                "completion_telemetry_runtime_artifact_case_invalid",
            ),
            (
                "case_reconciliation",
                lambda value: value["cases"][0].__setitem__(
                    "sdk_raw_response_reconciliation", "unavailable"
                ),
                "completion_telemetry_runtime_artifact_case_invalid",
            ),
            (
                "case_order",
                lambda value: value["cases"].reverse(),
                "completion_telemetry_runtime_artifact_case_invalid",
            ),
            (
                "nested_sdk_response_index",
                lambda value: value["cases"][0][
                    "sdk_request_usage_indices_by_response"
                ][0].__setitem__("response_index", 1),
                "completion_telemetry_runtime_artifact_case_invalid",
            ),
            (
                "record_index",
                lambda value: value["records"][1].__setitem__(
                    "request_index", 0
                ),
                "completion_telemetry_runtime_artifact_record_set_invalid",
            ),
            (
                "record_mapping",
                lambda value: value["records"][0].__setitem__(
                    "normalized_completion_state", "error"
                ),
                "completion_telemetry_mapping_result_mismatch",
            ),
            (
                "record_extra_field",
                lambda value: value["records"][0].__setitem__(
                    "response_body", "forbidden"
                ),
                "completion_telemetry_record_shape_invalid",
            ),
        )
        for label, mutate, expected_code in cases:
            with self.subTest(label=label):
                artifact = copy.deepcopy(original)
                mutate(artifact)
                self.assertInvalid(artifact, plan, expected_code)

    def test_denominator_subartifact_illegal_types_and_forged_binding_fail(
        self,
    ) -> None:
        plan, original = _accepted_artifact()
        cases = (
            (
                "top_bool_count",
                lambda value: value.__setitem__("attempts_started", True),
                "completion_telemetry_runtime_artifact_count_mismatch",
            ),
            (
                "plan_bool_limit",
                lambda value: value.__setitem__("max_turns_per_case", True),
                "completion_telemetry_runtime_artifact_plan_binding_mismatch",
            ),
            (
                "attempt_bool_index",
                lambda value: value["attempts"][0].__setitem__(
                    "attempt_index", True
                ),
                "completion_telemetry_runtime_artifact_attempt_invalid",
            ),
            (
                "case_bool_closure",
                lambda value: value["cases"][0].__setitem__(
                    "closure_eligible", 1
                ),
                "completion_telemetry_runtime_artifact_case_invalid",
            ),
        )
        for label, mutate, expected_code in cases:
            with self.subTest(label=label):
                artifact = copy.deepcopy(original)
                mutate(artifact)
                self.assertInvalid(artifact, plan, expected_code)

        self.assertInvalid(
            [],
            plan,
            "completion_telemetry_runtime_artifact_shape_invalid",
        )
        forged = object.__new__(VerifiedRuntimeDenominatorPlanBinding)
        self.assertInvalid(
            original,
            forged,
            "completion_telemetry_capture_plan_binding_required",
        )

    def test_denominator_subartifact_scans_canary_before_record_acceptance(
        self,
    ) -> None:
        plan, artifact = _accepted_artifact()
        artifact["records"][0]["native_stop_sequence"] = {
            "availability": "provided",
            "value": "DYNAMIC-ARTIFACT-CANARY-7391",
            "redaction_applied": False,
            "truncated": False,
        }
        with self.assertRaises(CompletionTelemetryError) as caught:
            validate_runtime_denominator_artifact(
                artifact,
                plan_binding=plan,
                sensitive_canaries=("DYNAMIC-ARTIFACT-CANARY-7391",),
            )
        self.assertEqual(
            caught.exception.code, "completion_telemetry_sensitive_value"
        )

    def test_matched_sdk_rows_reject_deletion_gap_and_duplicate(self) -> None:
        plan, original = _multi_response_artifact()
        mutations = {
            "deletion": lambda rows: rows.clear(),
            "gap": lambda rows: rows.pop(0),
            "duplicate": lambda rows: rows[1].update(
                {
                    "response_index": 0,
                    "sdk_raw_response_index": 0,
                }
            ),
        }
        for label, mutate in mutations.items():
            with self.subTest(label=label):
                artifact = copy.deepcopy(original)
                rows = artifact["cases"][0][
                    "sdk_request_usage_indices_by_response"
                ]
                mutate(rows)
                self.assertInvalid(
                    artifact,
                    plan,
                    "completion_telemetry_runtime_artifact_case_invalid",
                )


if __name__ == "__main__":
    unittest.main()
