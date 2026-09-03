from __future__ import annotations

import hashlib
import json
import unittest
from dataclasses import replace
from pathlib import Path

import researchops_completion_telemetry.surface_mapping as surface_mapping
from researchops_completion_telemetry.capture import (
    CompletionCaptureError,
    CompletionTelemetryCollector,
    RuntimeDenominatorTracker,
    evaluate_runtime_denominator_closure,
    verify_runtime_denominator_plan,
)
from researchops_completion_telemetry.sanitization import (
    sanitize_completion_capture,
)
from researchops_completion_telemetry.surface_mapping import (
    VerifiedRuntimeCompletionBinding,
    load_and_select_surface_mapping,
)


ROOT = Path(__file__).resolve().parents[1]


def _canonical_sha256(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()


def _test_runtime_binding() -> VerifiedRuntimeCompletionBinding:
    """Mint test-only runtime authority from already verified offline artifacts."""

    offline = load_and_select_surface_mapping(
        ROOT,
        "deepseek",
        "responses",
        "openai_compatible_responses",
        purpose="offline_validation",
    )
    entry = {
        "adapter_version": offline.adapter_version,
        "mapping_version": offline.mapping_version,
        "output_counter_comparability": offline.output_counter_comparability,
        "output_counter_path": offline.output_counter_path,
        "runtime_binding_allowed": True,
    }
    selection = surface_mapping.VerifiedSurfaceSelection._create(
        surface_mapping._SELECTION_TOKEN,
        purpose="runtime_binding",
        telemetry_schema_sha256=offline.telemetry_schema_sha256,
        mapping=offline.mapping_snapshot(),
        entry=entry,
    )
    return selection.create_runtime_binding()


def _plan_binding(
    *,
    case_ids: tuple[str, ...] = ("CASE-001", "CASE-002"),
    max_turns: int = 3,
    request_cap: int = 5,
):
    runtime = _test_runtime_binding()
    snapshot = runtime.runtime_snapshot()
    plan = {
        "schema_version": "provider-completion-runtime-denominator-plan/1.0",
        "provider_id": snapshot["provider_id"],
        "api_surface": snapshot["api_surface"],
        "transport_id": snapshot["transport_id"],
        "adapter_version": snapshot["adapter_version"],
        "telemetry_schema_sha256": snapshot["telemetry_schema_sha256"],
        "mapping_schema_version": snapshot["mapping_schema_version"],
        "mapping_version": snapshot["mapping_version"],
        "mapping_sha256": snapshot["mapping_sha256"],
        "case_ids": list(case_ids),
        "case_ids_sha256": _canonical_sha256(list(case_ids)),
        "max_turns_per_case": max_turns,
        "total_model_request_cap": request_cap,
        "agents_sdk_retries": 0,
        "http_client_retries": 0,
        "denominator_algorithm": "transport-response-finalization-v1",
        "exact_response_count_preregistered": False,
    }
    return verify_runtime_denominator_plan(
        runtime,
        plan,
        preregistration_commitment=_canonical_sha256(plan),
    )


def _capture(status: str = "completed"):
    details = None if status == "completed" else {"reason": "max_output_tokens"}
    return sanitize_completion_capture(
        {
            "status": status,
            "incomplete_details": details,
            "usage": {
                "input_tokens": 10,
                "output_tokens": 2,
                "total_tokens": 12,
            },
            "requested_output_token_cap": 2000,
        },
        normalized_usage={
            "requests": 1,
            "input_tokens": 10,
            "output_tokens": 2,
            "total_tokens": 12,
            "cached_input_tokens": None,
            "cache_write_tokens": None,
            "reasoning_tokens": None,
        },
    )


class ProviderCompletionDynamicDenominatorTests(unittest.TestCase):
    def test_max_turns_is_an_upper_bound_not_an_exact_response_count(self) -> None:
        plan = _plan_binding(max_turns=3, request_cap=5)
        tracker = CompletionTelemetryCollector.for_runtime(plan)
        self.assertIsInstance(tracker, RuntimeDenominatorTracker)
        handle = tracker.begin_attempt("CASE-001")
        terminal = tracker.finalize_response_accepted(handle, _capture())
        self.assertEqual((terminal.attempt_index, terminal.response_index), (0, 0))
        case = tracker.seal_case(
            "CASE-001",
            sdk_raw_response_count=1,
            sdk_usage_request_count=1,
            sdk_request_usage_indices_by_response={0: (0,)},
        )
        self.assertEqual(case.observed_response_count, 1)
        artifact = tracker.seal_runtime()
        projection = artifact.to_dict()
        self.assertFalse(projection["exact_response_count_preregistered"])
        self.assertTrue(projection["derived_after_run"])
        self.assertEqual(projection["max_turns_per_case"], 3)
        self.assertEqual(projection["observed_response_count"], 1)

    def test_plan_rejects_an_exact_response_count_field(self) -> None:
        runtime = _test_runtime_binding()
        snapshot = runtime.runtime_snapshot()
        plan = {
            "schema_version": "provider-completion-runtime-denominator-plan/1.0",
            "provider_id": snapshot["provider_id"],
            "api_surface": snapshot["api_surface"],
            "transport_id": snapshot["transport_id"],
            "adapter_version": snapshot["adapter_version"],
            "telemetry_schema_sha256": snapshot["telemetry_schema_sha256"],
            "mapping_schema_version": snapshot["mapping_schema_version"],
            "mapping_version": snapshot["mapping_version"],
            "mapping_sha256": snapshot["mapping_sha256"],
            "case_ids": ["CASE-001"],
            "case_ids_sha256": _canonical_sha256(["CASE-001"]),
            "max_turns_per_case": 3,
            "total_model_request_cap": 3,
            "agents_sdk_retries": 0,
            "http_client_retries": 0,
            "denominator_algorithm": "transport-response-finalization-v1",
            "exact_response_count_preregistered": False,
            "expected_response_count": 3,
        }
        with self.assertRaises(CompletionCaptureError):
            verify_runtime_denominator_plan(
                runtime,
                plan,
                preregistration_commitment=_canonical_sha256(plan),
            )
        plan.pop("expected_response_count")
        plan["agents_sdk_retries"] = False
        with self.assertRaises(CompletionCaptureError):
            verify_runtime_denominator_plan(
                runtime,
                plan,
                preregistration_commitment=_canonical_sha256(plan),
            )

    def test_pending_attempt_and_duplicate_terminal_fail_closed(self) -> None:
        tracker = RuntimeDenominatorTracker(_plan_binding())
        handle = tracker.begin_attempt("CASE-001")
        with self.assertRaises(CompletionCaptureError) as case:
            tracker.seal_case(
                "CASE-001",
                sdk_raw_response_count=0,
                sdk_usage_request_count=0,
            )
        self.assertEqual(case.exception.code, "completion_capture_attempt_pending")
        with self.assertRaises(CompletionCaptureError) as campaign:
            tracker.seal_runtime()
        self.assertEqual(campaign.exception.code, "completion_capture_attempt_pending")
        tracker.finalize_no_response(handle, "provider_connection_failed")
        with self.assertRaises(CompletionCaptureError) as duplicate:
            tracker.finalize_cancelled(handle)
        self.assertEqual(
            duplicate.exception.code, "completion_capture_attempt_already_terminal"
        )

    def test_case_session_cannot_finalize_another_cases_handle(self) -> None:
        tracker = RuntimeDenominatorTracker(_plan_binding())
        first_session = tracker.bind_case("CASE-001")
        second_session = tracker.bind_case("CASE-002")
        handle = first_session.begin_attempt()
        with self.assertRaises(CompletionCaptureError) as caught:
            second_session.finalize_no_response(handle, "provider_no_response")
        self.assertEqual(
            caught.exception.code, "completion_capture_attempt_handle_invalid"
        )
        first_session.finalize_no_response(handle, "provider_no_response")

    def test_case_and_campaign_attempt_caps_are_enforced(self) -> None:
        case_tracker = RuntimeDenominatorTracker(
            _plan_binding(case_ids=("CASE-001",), max_turns=2, request_cap=2)
        )
        for _ in range(2):
            handle = case_tracker.begin_attempt("CASE-001")
            case_tracker.finalize_no_response(handle, "provider_no_response")
        with self.assertRaises(CompletionCaptureError) as case_cap:
            case_tracker.begin_attempt("CASE-001")
        self.assertEqual(
            case_cap.exception.code, "completion_capture_case_attempt_cap_exceeded"
        )

        campaign = RuntimeDenominatorTracker(
            _plan_binding(max_turns=2, request_cap=2)
        )
        for case_id in ("CASE-001", "CASE-002"):
            handle = campaign.begin_attempt(case_id)
            campaign.finalize_no_response(handle, "provider_no_response")
        with self.assertRaises(CompletionCaptureError) as total_cap:
            campaign.begin_attempt("CASE-001")
        self.assertEqual(
            total_cap.exception.code, "completion_capture_campaign_attempt_cap_exceeded"
        )

    def test_no_response_does_not_consume_response_index(self) -> None:
        tracker = RuntimeDenominatorTracker(_plan_binding())
        first = tracker.begin_attempt("CASE-001")
        no_response = tracker.finalize_no_response(first, "provider_no_response")
        second = tracker.begin_attempt("CASE-001")
        accepted = tracker.finalize_response_accepted(second, _capture())
        self.assertIsNone(no_response.response_index)
        self.assertEqual(accepted.response_index, 0)
        case = tracker.seal_case(
            "CASE-001", sdk_raw_response_count=1, sdk_usage_request_count=2
        )
        self.assertFalse(case.closure_eligible)

    def test_sdk_count_mismatch_and_unavailable_forbid_closure(self) -> None:
        for sdk_count, expected in ((0, "mismatched"), (None, "unavailable")):
            with self.subTest(sdk_count=sdk_count):
                tracker = RuntimeDenominatorTracker(
                    _plan_binding(case_ids=("CASE-001",), request_cap=3)
                )
                handle = tracker.begin_attempt("CASE-001")
                tracker.finalize_response_accepted(handle, _capture())
                case = tracker.seal_case(
                    "CASE-001",
                    sdk_raw_response_count=sdk_count,
                    sdk_usage_request_count=1,
                )
                self.assertEqual(case.sdk_raw_response_reconciliation, expected)
                closure = evaluate_runtime_denominator_closure(
                    tracker.seal_runtime(), [("completed", "native_status")]
                )
                self.assertFalse(closure["claim_allowed"])
                expected_reason = (
                    "sdk_raw_response_count_unavailable"
                    if sdk_count is None
                    else "sdk_raw_response_count_mismatched"
                )
                self.assertIn(expected_reason, closure["reasons"])

    def test_adapter_case_session_and_successful_reconciliation(self) -> None:
        tracker = RuntimeDenominatorTracker(
            _plan_binding(case_ids=("CASE-001",), max_turns=3, request_cap=3)
        )
        session = tracker.bind_case("CASE-001")
        self.assertEqual(session.case_id, "CASE-001")
        self.assertEqual(session.provider_id, "deepseek")
        self.assertEqual(session.api_surface, "responses")
        self.assertEqual(session.transport_id, "openai_compatible_responses")
        self.assertEqual(session.adapter_version, "deepseek-responses-adapter/1.0")
        self.assertEqual(
            session.binding_snapshot()["mapping_sha256"],
            _plan_binding(
                case_ids=("CASE-001",), max_turns=3, request_cap=3
            ).runtime_binding().mapping_sha256,
        )
        first = session.begin_attempt()
        session.finalize_response_accepted(first, _capture())
        second = session.begin_attempt()
        session.finalize_response_accepted(second, _capture("incomplete"))
        case = tracker.seal_case(
            "CASE-001",
            sdk_raw_response_count=2,
            sdk_usage_request_count=2,
            sdk_request_usage_indices_by_response={0: (0,), 1: (0, 1)},
        )
        self.assertTrue(case.closure_eligible)
        self.assertEqual(
            case.sdk_request_usage_indices_by_response,
            ((0, 0, (0,)), (1, 1, (0, 1))),
        )
        closure = evaluate_runtime_denominator_closure(
            tracker.seal_runtime(),
            [
                ("completed", "native_status"),
                ("incomplete_length", "native_status"),
            ],
        )
        self.assertTrue(closure["claim_allowed"])
        self.assertEqual(closure["observed_response_count"], 2)

    def test_unfinalized_planned_case_forbids_campaign_closure(self) -> None:
        tracker = RuntimeDenominatorTracker(_plan_binding())
        handle = tracker.begin_attempt("CASE-001")
        tracker.finalize_response_accepted(handle, _capture())
        tracker.seal_case(
            "CASE-001", sdk_raw_response_count=1, sdk_usage_request_count=1
        )
        artifact = tracker.seal_runtime()
        self.assertEqual(artifact.not_finalized_case_ids, ("CASE-002",))
        closure = evaluate_runtime_denominator_closure(
            artifact, [("completed", "native_status")]
        )
        self.assertFalse(closure["claim_allowed"])
        self.assertIn("planned_cases_not_finalized", closure["reasons"])

    def test_sdk_response_and_nested_usage_indices_do_not_reuse_global_attempt_index(self) -> None:
        tracker = RuntimeDenominatorTracker(_plan_binding())
        first = tracker.begin_attempt("CASE-001")
        tracker.finalize_no_response(first, "provider_no_response")
        tracker.seal_case(
            "CASE-001", sdk_raw_response_count=0, sdk_usage_request_count=1
        )
        second = tracker.begin_attempt("CASE-002")
        terminal = tracker.finalize_response_accepted(second, _capture())
        self.assertEqual(terminal.attempt_index, 1)
        self.assertEqual(terminal.response_index, 0)
        case = tracker.seal_case(
            "CASE-002",
            sdk_raw_response_count=1,
            sdk_usage_request_count=1,
            sdk_request_usage_indices_by_response={0: (0, 1)},
        )
        self.assertEqual(
            case.sdk_request_usage_indices_by_response,
            ((0, 0, (0, 1)),),
        )

    def test_runtime_artifact_recomputes_counts_and_terminal_semantics(self) -> None:
        tracker = RuntimeDenominatorTracker(
            _plan_binding(case_ids=("CASE-001",), request_cap=3)
        )
        handle = tracker.begin_attempt("CASE-001")
        tracker.finalize_response_accepted(handle, _capture())
        tracker.seal_case(
            "CASE-001", sdk_raw_response_count=1, sdk_usage_request_count=1
        )
        artifact = tracker.seal_runtime()
        with self.assertRaises(CompletionCaptureError):
            replace(artifact, observed_response_count=2).to_dict()
        bad_terminal = replace(
            artifact.attempts[0], terminal_kind="outcome_unknown", error_code=None
        )
        with self.assertRaises(CompletionCaptureError):
            replace(artifact, attempts=(bad_terminal,)).to_dict()


if __name__ == "__main__":
    unittest.main()
