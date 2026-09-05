from __future__ import annotations

import json
import unittest
from pathlib import Path

from researchops_completion_telemetry.capture import (
    RUNTIME_CLOSURE_RECOGNIZED_STATES,
    RUNTIME_CLOSURE_REQUIRED_SIGNAL_SOURCE,
    RuntimeDenominatorTracker,
    evaluate_runtime_denominator_closure,
)
from researchops_completion_telemetry.mapping import may_claim_truncation_excluded
from researchops_completion_telemetry.sanitization import sanitize_completion_capture
from tests.test_provider_completion_dynamic_denominator import _capture, _plan_binding


ROOT = Path(__file__).resolve().parents[1]
MAPPING_PATH = (
    ROOT
    / "evals/provider_completion_telemetry_v1/provider_completion_mapping_v1.json"
)
CONTRACT_PATH = (
    ROOT
    / "evals/provider_completion_telemetry_v1/provider_completion_record_contract_v1.json"
)


def _accepted_artifact(*, capture=None, raw_count=1, usage_count=1):
    tracker = RuntimeDenominatorTracker(
        _plan_binding(case_ids=("CASE-001",), max_turns=3, request_cap=3)
    )
    handle = tracker.begin_attempt("CASE-001")
    tracker.finalize_response_accepted(handle, capture or _capture())
    tracker.seal_case(
        "CASE-001",
        sdk_raw_response_count=raw_count,
        sdk_usage_request_count=usage_count,
    )
    return tracker.seal_runtime()


def _missing_native_capture(*, at_cap: bool = False):
    output_tokens = 2000 if at_cap else 2
    return sanitize_completion_capture(
        {
            "usage": {
                "input_tokens": 10,
                "output_tokens": output_tokens,
                "total_tokens": 10 + output_tokens,
            },
            "requested_output_token_cap": 2000,
        },
        normalized_usage={
            "requests": 1,
            "input_tokens": 10,
            "output_tokens": output_tokens,
            "total_tokens": 10 + output_tokens,
            "cached_input_tokens": None,
            "cache_write_tokens": None,
            "reasoning_tokens": None,
        },
    )


class ProviderCompletionClosureMatrixTests(unittest.TestCase):
    def assertClosed(self, artifact, *expected_reasons: str, projection=None):
        result = evaluate_runtime_denominator_closure(artifact, projection)
        self.assertFalse(result["claim_allowed"])
        for reason in expected_reasons:
            self.assertIn(reason, result["reasons"])
        return result

    def test_zero_attempts_and_zero_observed_have_distinct_reasons(self) -> None:
        tracker = RuntimeDenominatorTracker(
            _plan_binding(case_ids=("CASE-001",), max_turns=3, request_cap=3)
        )
        tracker.seal_case(
            "CASE-001", sdk_raw_response_count=0, sdk_usage_request_count=0
        )
        result = self.assertClosed(
            tracker.seal_runtime(),
            "no_model_attempts_observed",
            "no_provider_responses_observed",
        )
        self.assertEqual(result["observed_response_count"], 0)

    def test_each_nonaccepted_attempt_terminal_has_an_exact_reason(self) -> None:
        cases = {
            "response_rejected": (
                "response_telemetry_rejected",
                1,
            ),
            "no_response": ("request_failed_without_response", 0),
            "http_error": ("http_error_response_observed", 0),
            "cancelled": ("model_request_cancelled", 0),
            "outcome_unknown": ("model_request_outcome_unknown", 0),
        }
        for terminal_kind, (expected_reason, raw_count) in cases.items():
            with self.subTest(terminal_kind=terminal_kind):
                tracker = RuntimeDenominatorTracker(
                    _plan_binding(
                        case_ids=("CASE-001",), max_turns=3, request_cap=3
                    )
                )
                handle = tracker.begin_attempt("CASE-001")
                if terminal_kind == "response_rejected":
                    tracker.finalize_response_rejected(
                        handle, "provider_completion_capture_failed"
                    )
                elif terminal_kind == "no_response":
                    tracker.finalize_no_response(handle, "provider_no_response")
                elif terminal_kind == "http_error":
                    tracker.finalize_http_error(handle, "provider_http_error")
                elif terminal_kind == "cancelled":
                    tracker.finalize_cancelled(handle)
                else:
                    tracker.finalize_outcome_unknown(
                        handle, "provider_outcome_unknown"
                    )
                tracker.seal_case(
                    "CASE-001",
                    sdk_raw_response_count=raw_count,
                    sdk_usage_request_count=1,
                )
                self.assertClosed(tracker.seal_runtime(), expected_reason)

    def test_sdk_raw_and_usage_count_matrix_has_exact_reasons(self) -> None:
        cases = (
            (0, 1, "sdk_raw_response_count_mismatched"),
            (None, 1, "sdk_raw_response_count_unavailable"),
            (1, 0, "sdk_usage_request_count_mismatched"),
            (1, None, "sdk_usage_request_count_unavailable"),
        )
        for raw_count, usage_count, expected_reason in cases:
            with self.subTest(expected_reason=expected_reason):
                self.assertClosed(
                    _accepted_artifact(
                        raw_count=raw_count, usage_count=usage_count
                    ),
                    expected_reason,
                )

    def test_unfinalized_planned_case_has_exact_reason(self) -> None:
        tracker = RuntimeDenominatorTracker(_plan_binding())
        handle = tracker.begin_attempt("CASE-001")
        tracker.finalize_response_accepted(handle, _capture())
        tracker.seal_case(
            "CASE-001", sdk_raw_response_count=1, sdk_usage_request_count=1
        )
        self.assertClosed(
            tracker.seal_runtime(), "planned_cases_not_finalized"
        )

    def test_completion_state_and_signal_negative_matrix(self) -> None:
        unmapped = _accepted_artifact(capture=_capture("failed"))
        self.assertClosed(unmapped, "completion_state_unmapped")

        missing = _accepted_artifact(capture=_missing_native_capture())
        missing_result = self.assertClosed(
            missing,
            "completion_state_not_provided",
            "truncation_signal_none",
        )
        self.assertEqual(missing_result["observed_response_count"], 1)

        fallback = _accepted_artifact(capture=_missing_native_capture(at_cap=True))
        self.assertClosed(fallback, "truncation_signal_token_cap_fallback")

    def test_external_state_source_projection_mismatch_is_not_authoritative(self) -> None:
        artifact = _accepted_artifact()
        result = self.assertClosed(
            artifact,
            "completion_state_projection_mismatch",
            projection=[("error", "native_status")],
        )
        self.assertNotIn("completion_state_unrecognized", result["reasons"])

    def test_mapping_contract_and_dynamic_closure_policy_are_identical(self) -> None:
        mapping = json.loads(MAPPING_PATH.read_text(encoding="utf-8"))
        contract = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))
        policy = mapping["global_rules"]["additional_truncation_exclusion_claim"]
        mapping_states = set(
            policy["allowed_only_if_every_response_normalized_completion_state_in"]
        )
        contract_states = set(contract["closing_gate"]["recognized_terminal_states"])
        self.assertEqual(mapping_states, contract_states)
        self.assertEqual(mapping_states, set(RUNTIME_CLOSURE_RECOGNIZED_STATES))
        self.assertEqual(
            policy[
                "allowed_only_if_every_response_truncation_signal_source_equals"
            ],
            RUNTIME_CLOSURE_REQUIRED_SIGNAL_SOURCE,
        )
        for state in sorted(mapping_states):
            with self.subTest(state=state):
                self.assertTrue(
                    may_claim_truncation_excluded(
                        [(state, RUNTIME_CLOSURE_REQUIRED_SIGNAL_SOURCE)], mapping
                    )
                )


if __name__ == "__main__":
    unittest.main()
