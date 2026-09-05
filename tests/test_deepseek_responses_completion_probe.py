from __future__ import annotations

import ast
import asyncio
import contextlib
import hashlib
import importlib.util
import io
import json
import logging
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = ROOT / "scripts" / "probe_deepseek_responses_completion.py"
SPEC = importlib.util.spec_from_file_location(
    "probe_deepseek_responses_completion",
    SCRIPT_PATH,
)
assert SPEC is not None and SPEC.loader is not None
probe = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = probe
SPEC.loader.exec_module(probe)


class FakeRawResponse:
    def __init__(
        self,
        body: dict[str, object],
        *,
        request_id: str | None,
        status_code: int = 200,
    ) -> None:
        self.content = json.dumps(
            body,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        self.headers = {} if request_id is None else {"x-request-id": request_id}
        self.status_code = status_code
        self.closed = False
        self.http_response = SimpleNamespace(aclose=self._aclose)

    async def _aclose(self) -> None:
        self.closed = True


class FakeCreate:
    def __init__(self, responses: list[FakeRawResponse]) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, object]] = []

    async def __call__(self, **kwargs: object) -> FakeRawResponse:
        self.calls.append(dict(kwargs))
        return self.responses.pop(0)


def _fake_client(responses: list[FakeRawResponse]) -> tuple[object, FakeCreate]:
    create = FakeCreate(responses)
    client = SimpleNamespace(
        responses=SimpleNamespace(
            with_raw_response=SimpleNamespace(create=create),
        )
    )
    return client, create


class DeepSeekResponsesCompletionProbeTests(unittest.TestCase):
    def test_projection_preserves_missing_versus_explicit_null(self) -> None:
        body = {
            "status": None,
            "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
        }
        projection = probe._project(body)
        self.assertIn("status", projection)
        self.assertIsNone(projection["status"])
        self.assertNotIn("incomplete_details", projection)
        self.assertEqual(probe._presence(body, "status"), "present_null")
        self.assertEqual(probe._presence(body, "incomplete_details"), "missing")
        self.assertEqual(
            probe._incomplete_reason_presence(body),
            "parent_missing",
        )

    def test_raw_probe_path_preserves_missing_versus_null_end_to_end(self) -> None:
        missing = FakeRawResponse(
            {"usage": None, "output": []},
            request_id=None,
        )
        explicit_null = FakeRawResponse(
            {
                "status": None,
                "incomplete_details": None,
                "usage": None,
                "output": [],
            },
            request_id=None,
        )
        message_stage = FakeRawResponse(
            {
                "status": "incomplete",
                "incomplete_details": {"reason": "max_output_tokens"},
                "usage": None,
                "output": [{"type": "message", "status": "incomplete"}],
            },
            request_id=None,
        )
        client, _ = _fake_client([missing, explicit_null, message_stage])

        receipt = asyncio.run(probe.run_probes(client))
        missing_record, null_record, _ = receipt["probes"]

        self.assertNotIn("status", missing_record["top_level_keys_observed"])
        self.assertNotIn(
            "incomplete_details", missing_record["top_level_keys_observed"]
        )
        self.assertEqual(
            missing_record["top_level_presence"],
            {
                "status": "missing",
                "incomplete_details": "missing",
                "stop_sequence": "missing",
            },
        )
        self.assertNotIn("status", missing_record["response_projection"])
        self.assertNotIn(
            "incomplete_details", missing_record["response_projection"]
        )

        self.assertIn("status", null_record["top_level_keys_observed"])
        self.assertIn("incomplete_details", null_record["top_level_keys_observed"])
        self.assertEqual(null_record["top_level_presence"]["status"], "present_null")
        self.assertEqual(
            null_record["top_level_presence"]["incomplete_details"],
            "present_null",
        )
        self.assertIn("status", null_record["response_projection"])
        self.assertIsNone(null_record["response_projection"]["status"])
        self.assertIn("incomplete_details", null_record["response_projection"])
        self.assertIsNone(null_record["response_projection"]["incomplete_details"])
        self.assertTrue(missing.closed)
        self.assertTrue(explicit_null.closed)
        self.assertTrue(message_stage.closed)

    def test_three_raw_response_shapes_are_sanitized_and_requests_are_exact(self) -> None:
        first = FakeRawResponse(
            {
                "id": "raw-completion-id-must-not-survive",
                "status": "completed",
                "incomplete_details": None,
                "content_filters": [],
                "error": None,
                "truncation": "truncation-value-must-not-survive",
                "stop_sequence": "stop-sequence-content-must-not-survive",
                "usage": {
                    "input_tokens": 8,
                    "output_tokens": 1,
                    "total_tokens": 9,
                    "account_label": "usage-string-must-not-survive",
                },
                "output": [
                    {
                        "id": "raw-item-id-must-not-survive",
                        "type": "message",
                        "status": "completed",
                        "content": "raw-content-must-not-survive",
                        "arguments": "raw-arguments-must-not-survive",
                    }
                ],
            },
            request_id="req-safe-normal",
        )
        second = FakeRawResponse(
            {
                "id": "raw-truncated-id-must-not-survive",
                "status": "incomplete",
                "incomplete_details": {
                    "reason": "max_output_tokens",
                    "message": "details-message-must-not-survive",
                },
                "content_filters": {
                    "nested-key-must-not-survive": "nested-value-must-not-survive"
                },
                "error": {
                    "code": "error-code-must-not-survive",
                    "message": "error-message-must-not-survive",
                },
                "truncation": None,
                "usage": {
                    "input_tokens": 16,
                    "output_tokens": 16,
                    "total_tokens": 32,
                    "output_tokens_details": {
                        "reasoning_tokens": 3,
                        "identifier": "usage-detail-id-must-not-survive",
                    },
                },
                "output": [
                    {
                        "type": "message",
                        "status": "incomplete",
                        "content": "truncated-content-must-not-survive",
                    }
                ],
            },
            request_id="req-safe-capped",
        )
        third = FakeRawResponse(
            {
                "status": "incomplete",
                "incomplete_details": {"reason": "max_output_tokens"},
                "content_filters": None,
                "error": [],
                "truncation": "third-truncation-value-must-not-survive",
                "usage": {
                    "input_tokens": 24,
                    "output_tokens": 96,
                    "total_tokens": 120,
                    "output_tokens_details": {"reasoning_tokens": 8},
                },
                "output": [
                    {"type": "reasoning", "status": "completed"},
                    {
                        "type": "message",
                        "status": "incomplete",
                        "content": "message-stage-content-must-not-survive",
                    },
                ],
            },
            request_id="req-safe-message-stage",
        )
        client, create = _fake_client([first, second, third])
        self.assertFalse(hasattr(first, "close"))

        with patch("builtins.open", side_effect=AssertionError("file I/O forbidden")):
            receipt = asyncio.run(probe.run_probes(client))

        self.assertEqual(len(create.calls), 3)
        self.assertEqual(
            create.calls[0],
            {
                "model": "deepseek-v4-flash",
                "input": "Reply with the single word: ok",
                "max_output_tokens": 256,
                "stream": False,
            },
        )
        self.assertEqual(
            create.calls[1],
            {
                "model": "deepseek-v4-flash",
                "input": "Count upward from one, one number per line, without stopping.",
                "max_output_tokens": 16,
                "stream": False,
            },
        )
        self.assertEqual(
            create.calls[2],
            {
                "model": "deepseek-v4-flash",
                "input": probe.MESSAGE_STAGE_INSTRUCTION,
                "max_output_tokens": 96,
                "stream": False,
            },
        )
        self.assertTrue(first.closed)
        self.assertTrue(second.closed)
        self.assertTrue(third.closed)

        records = receipt["probes"]
        self.assertTrue(
            all(
                record["raw_response_cleanup_state"] == "succeeded"
                for record in records
            )
        )
        self.assertEqual(records[0]["top_level_presence"]["status"], "present_non_null")
        self.assertEqual(
            records[0]["top_level_presence"]["incomplete_details"],
            "present_null",
        )
        self.assertEqual(
            records[1]["incomplete_details_reason_presence"],
            "reason_present_non_null",
        )
        self.assertEqual(
            records[1]["response_projection"]["incomplete_details"],
            {"reason": "max_output_tokens"},
        )
        self.assertNotIn("stop_sequence", records[0]["response_projection"])
        self.assertEqual(
            records[1]["incomplete_details_keys_observed"],
            ["message", "reason"],
        )
        self.assertEqual(
            records[0]["usage_keys_observed"],
            ["account_label", "input_tokens", "output_tokens", "total_tokens"],
        )
        self.assertEqual(
            records[0]["provider_request_id_sha256"],
            hashlib.sha256(b"req-safe-normal").hexdigest(),
        )
        self.assertEqual(
            records[0]["output_item_shapes"],
            [
                {
                    "index": 0,
                    "status": "completed",
                    "status_key_present": True,
                    "type": "message",
                }
            ],
        )
        self.assertEqual(
            records[0]["shape_only_top_level_observations"],
            {
                "content_filters": {
                    "presence": "present_non_null",
                    "json_type": "array",
                    "direct_child_count": 0,
                },
                "error": {
                    "presence": "present_null",
                    "json_type": "null",
                    "direct_child_count": None,
                },
                "truncation": {
                    "presence": "present_non_null",
                    "json_type": "string",
                    "direct_child_count": None,
                },
            },
        )
        self.assertEqual(
            records[1]["shape_only_top_level_observations"]["content_filters"],
            {
                "presence": "present_non_null",
                "json_type": "object",
                "direct_child_count": 1,
            },
        )
        self.assertEqual(
            records[2]["output_item_shapes"],
            [
                {
                    "index": 0,
                    "status": "completed",
                    "status_key_present": True,
                    "type": "reasoning",
                },
                {
                    "index": 1,
                    "status": "incomplete",
                    "status_key_present": True,
                    "type": "message",
                },
            ],
        )
        self.assertTrue(
            receipt["limitations"]["message_stage_cap_target_observed"]
        )
        serialized = probe._canonical_json(receipt)
        for forbidden in (
            "raw-completion-id-must-not-survive",
            "raw-item-id-must-not-survive",
            "raw-content-must-not-survive",
            "raw-arguments-must-not-survive",
            "truncated-content-must-not-survive",
            "stop-sequence-content-must-not-survive",
            "usage-string-must-not-survive",
            "details-message-must-not-survive",
            "usage-detail-id-must-not-survive",
            "nested-key-must-not-survive",
            "nested-value-must-not-survive",
            "error-code-must-not-survive",
            "error-message-must-not-survive",
            "truncation-value-must-not-survive",
            "third-truncation-value-must-not-survive",
            "message-stage-content-must-not-survive",
            "Reply with the single word",
            "Count upward from one",
        ):
            self.assertNotIn(forbidden, serialized)
        self.assertNotIn(probe.MESSAGE_STAGE_INSTRUCTION, serialized)

    def test_receipt_publishes_all_three_predeclared_limitations(self) -> None:
        receipt = probe._receipt([])
        self.assertEqual(
            receipt["limitations"],
            {
                "forced_cap_minimum_output_tokens": 16,
                "forced_cap_attempt_completed": False,
                "forced_cap_attempt_retained_status": None,
                "forced_cap_not_observed_interpretation_scope": (
                    "applies_only_when_no_truncation_was_observed"
                ),
                "forced_cap_not_observed_interpretation": (
                    "not_triggered_not_evidence_of_nonexistence"
                ),
                "adapter_kwargs_equivalence_claimed": False,
                "sdk_built_kwargs_offline_diff_performed": False,
                "known_but_unobserved_values_remain_unmapped": True,
                "supersedes_probe_receipt_sha256": (
                    "d124a07f40b1031247a832409bbf13f9c352e42862f3d4c64da6690eb89709ad"
                ),
                "superseded_field": "observed_shape_count_max",
                "superseded_field_reason": (
                    "counted_requests_not_distinct_shapes"
                ),
                "message_stage_probe_max_output_tokens": 96,
                "message_stage_probe_cap_is_fixed_non_adaptive": True,
                "message_stage_entry_guaranteed": False,
                "message_stage_requested_word_repetitions": 500,
                "message_stage_final_planned_attempt": True,
                "no_further_message_stage_probe_if_target_not_observed": True,
                "input_token_hard_cap_claimed": False,
                "message_stage_cap_not_observed_interpretation_scope": (
                    "applies_only_when_message_stage_cap_target_observed_is_false"
                ),
                "message_stage_cap_not_observed_interpretation": (
                    "not_triggered_not_evidence_of_nonexistence"
                ),
                "message_stage_cap_attempt_completed": False,
                "message_output_item_observed": False,
                "message_output_item_with_incomplete_status_observed": False,
                "top_level_incomplete_max_output_tokens_observed": False,
                "message_stage_cap_target_observed": False,
                "observed_response_count": 0,
                "observed_response_count_definition": (
                    "sanitized_response_shape_records_present_in_this_receipt"
                ),
                "distinct_top_level_shape_count": 0,
                "distinct_top_level_shape_eligible_response_count": 0,
                "distinct_top_level_shape_unavailable_response_count": 0,
                "distinct_shape_definition": probe.DISTINCT_SHAPE_DEFINITION,
            },
        )
        self.assertEqual(
            receipt["transport"]["kwargs_source"],
            "minimal_direct_probe_not_sdk_built",
        )
        self.assertEqual(
            receipt["transport"][
                "request_phase_plus_post_request_cleanup_timeout_upper_bound_seconds"
            ],
            310.0,
        )
        self.assertFalse(receipt["transport"]["setup_time_bounded"])
        self.assertFalse(receipt["transport"]["whole_process_wall_timeout_claimed"])
        self.assertFalse(receipt["boundary"]["response_body_persisted"])
        self.assertFalse(receipt["boundary"]["input_content_persisted"])
        self.assertTrue(receipt["boundary"]["provider_side_retention_unverified"])
        self.assertFalse(
            receipt["boundary"]["shape_only_top_level_values_persisted"]
        )
        self.assertTrue(
            receipt["boundary"][
                "shape_only_top_level_presence_type_and_count_persisted"
            ]
        )
        self.assertEqual(receipt["transport"]["network_attempts_max"], 3)
        self.assertEqual(receipt["transport"]["model_requests_max"], 3)
        self.assertEqual(receipt["transport"]["concurrency"], 1)
        self.assertFalse(receipt["transport"]["resume"])
        self.assertFalse(receipt["transport"]["fallback"])
        self.assertEqual(
            receipt["transport"]["requested_max_output_tokens_sum_max"],
            368,
        )
        self.assertTrue(
            receipt["transport"][
                "request_phase_total_timeout_preempts_per_request_timeouts"
            ]
        )
        self.assertEqual(
            receipt["transport"]["maximum_raw_response_cleanup_resources"],
            3,
        )
        self.assertTrue(
            receipt["transport"][
                "raw_response_cleanup_included_in_request_phase_total_timeout"
            ]
        )
        self.assertEqual(
            receipt["transport"]["maximum_post_request_cleanup_resources"],
            2,
        )
        self.assertNotIn("maximum_cleanup_resources", receipt["transport"])
        self.assertEqual(receipt["probe_id"], "deepseek_responses_completion_shape_v3")
        self.assertIn("exactly 500 times", probe.MESSAGE_STAGE_INSTRUCTION)
        self.assertNotIn("probe-001", probe.MESSAGE_STAGE_INSTRUCTION)

    def test_failure_receipts_publish_the_same_predeclared_limitations(self) -> None:
        retained_records = [
            {
                "probe_label": "responses_normal_completion",
                "response_projection": {"status": "completed"},
                "incomplete_details_reason_presence": "parent_null",
                "output_item_shapes": [
                    {"type": "reasoning", "status": "completed"},
                    {"type": "message", "status": "completed"},
                ],
                "top_level_presence": {
                    "status": "present_non_null",
                    "incomplete_details": "present_null",
                    "stop_sequence": "missing",
                },
            }
        ]
        reference = probe._receipt(retained_records)
        failure = probe._failure_receipt(
            "deepseek_responses_total_timeout",
            outcome_unknown=True,
            retained_probe_record_count=1,
            retained_probe_records=retained_records,
        )
        self.assertEqual(failure["boundary"], reference["boundary"])
        self.assertEqual(failure["transport"], reference["transport"])
        self.assertEqual(failure["limitations"], reference["limitations"])
        self.assertEqual(
            failure["retained_probe_record_definition"],
            "sanitized_record_built_not_full_probe_lifecycle_completed",
        )
        self.assertEqual(
            failure["limitations"]["forced_cap_not_observed_interpretation"],
            "not_triggered_not_evidence_of_nonexistence",
        )
        self.assertEqual(
            failure["transport"]["kwargs_source"],
            "minimal_direct_probe_not_sdk_built",
        )
        self.assertEqual(failure["limitations"]["observed_response_count"], 1)
        self.assertEqual(
            failure["limitations"]["distinct_top_level_shape_count"],
            1,
        )
        self.assertTrue(failure["retained_probe_records"])

    def test_limitations_report_the_forced_cap_attempt_status_verbatim(self) -> None:
        truncated = probe._receipt(
            [
                {
                    "probe_label": "responses_normal_completion",
                    "response_projection": {"status": "completed"},
                },
                {
                    "probe_label": "responses_output_cap_attempt",
                    "response_projection": {"status": "incomplete"},
                },
            ]
        )
        self.assertTrue(truncated["limitations"]["forced_cap_attempt_completed"])
        self.assertEqual(
            truncated["limitations"]["forced_cap_attempt_retained_status"],
            "incomplete",
        )
        self.assertEqual(
            truncated["limitations"][
                "forced_cap_not_observed_interpretation_scope"
            ],
            "applies_only_when_no_truncation_was_observed",
        )
        absent = probe._receipt([])
        self.assertEqual(
            set(truncated["limitations"]),
            set(absent["limitations"]),
        )
        self.assertFalse(absent["limitations"]["forced_cap_attempt_completed"])
        self.assertIsNone(
            absent["limitations"]["forced_cap_attempt_retained_status"]
        )
        failed_after_cap = probe._failure_receipt(
            "deepseek_responses_client_close_failed",
            outcome_unknown=False,
            retained_probe_records=truncated["probes"],
        )
        self.assertTrue(
            failed_after_cap["limitations"]["forced_cap_attempt_completed"]
        )
        self.assertEqual(
            failed_after_cap["limitations"]["forced_cap_attempt_retained_status"],
            "incomplete",
        )
        self.assertEqual(
            set(failed_after_cap["limitations"]),
            set(absent["limitations"]),
        )
        for projection in ({}, {"status": None}):
            with self.subTest(projection=projection):
                completed_without_string_status = probe._receipt(
                    [
                        {
                            "probe_label": "responses_output_cap_attempt",
                            "response_projection": projection,
                        }
                    ]
                )
                self.assertTrue(
                    completed_without_string_status["limitations"][
                        "forced_cap_attempt_completed"
                    ]
                )
                self.assertIsNone(
                    completed_without_string_status["limitations"][
                        "forced_cap_attempt_retained_status"
                    ]
                )

    def test_message_stage_observation_requires_all_exact_native_signals(self) -> None:
        base = {
            "probe_label": probe.MESSAGE_STAGE_PROBE_LABEL,
            "response_projection": {
                "status": "incomplete",
                "incomplete_details": {"reason": "max_output_tokens"},
            },
            "output_item_shapes": [
                {"type": "message", "status": "incomplete"}
            ],
        }
        observed = probe._receipt([base])["limitations"]
        self.assertTrue(observed["message_stage_cap_attempt_completed"])
        self.assertTrue(observed["message_output_item_observed"])
        self.assertTrue(
            observed["message_output_item_with_incomplete_status_observed"]
        )
        self.assertTrue(
            observed["top_level_incomplete_max_output_tokens_observed"]
        )
        self.assertTrue(observed["message_stage_cap_target_observed"])

        variants = [
            {**base, "output_item_shapes": [{"type": "reasoning"}]},
            {
                **base,
                "output_item_shapes": [
                    {"type": "message", "status": "completed"}
                ],
            },
            {
                **base,
                "response_projection": {
                    "status": "completed",
                    "incomplete_details": None,
                },
            },
        ]
        for variant in variants:
            with self.subTest(variant=variant):
                limitations = probe._receipt([variant])["limitations"]
                self.assertTrue(limitations["message_stage_cap_attempt_completed"])
                self.assertFalse(limitations["message_stage_cap_target_observed"])
        absent = probe._receipt([])["limitations"]
        self.assertFalse(absent["message_stage_cap_attempt_completed"])
        self.assertFalse(absent["message_stage_cap_target_observed"])

    def test_distinct_shape_count_ignores_request_cap_label_and_usage(self) -> None:
        normal = {
            "probe_label": "responses_normal_completion",
            "requested_max_output_tokens": 256,
            "response_projection": {
                "status": "completed",
                "incomplete_details": None,
                "usage": {"output_tokens": 31},
            },
            "incomplete_details_reason_presence": "parent_null",
            "output_item_shapes": [
                {"type": "reasoning", "status": "completed"},
                {"type": "message", "status": "completed"},
            ],
            "top_level_presence": {
                "status": "present_non_null",
                "incomplete_details": "present_null",
                "stop_sequence": "missing",
            },
        }
        capped = {
            "probe_label": "responses_output_cap_attempt",
            "requested_max_output_tokens": 16,
            "response_projection": {
                "status": "incomplete",
                "incomplete_details": {"reason": "max_output_tokens"},
                "usage": {"output_tokens": 16},
            },
            "incomplete_details_reason_presence": "reason_present_non_null",
            "output_item_shapes": [
                {"type": "reasoning", "status": "incomplete"}
            ],
            "top_level_presence": {
                "status": "present_non_null",
                "incomplete_details": "present_non_null",
                "stop_sequence": "missing",
            },
        }
        same_shape_different_request = {
            **capped,
            "probe_label": probe.MESSAGE_STAGE_PROBE_LABEL,
            "requested_max_output_tokens": 96,
            "response_projection": {
                **capped["response_projection"],
                "usage": {"output_tokens": 96},
            },
        }
        counts = probe._shape_counts(
            [normal, capped, same_shape_different_request]
        )
        self.assertEqual(counts["observed_response_count"], 3)
        self.assertEqual(counts["distinct_top_level_shape_count"], 2)
        self.assertEqual(
            counts["distinct_top_level_shape_eligible_response_count"],
            3,
        )
        self.assertEqual(
            counts["distinct_top_level_shape_unavailable_response_count"],
            0,
        )
        self.assertEqual(
            counts["distinct_shape_definition"],
            probe.DISTINCT_SHAPE_DEFINITION,
        )
        self.assertNotIn("observed_shape_count_max", counts)

        with_unavailable = probe._shape_counts(
            [normal, capped, same_shape_different_request, {"probe_label": "x"}]
        )
        self.assertEqual(with_unavailable["observed_response_count"], 4)
        self.assertEqual(with_unavailable["distinct_top_level_shape_count"], 2)
        self.assertEqual(
            with_unavailable["distinct_top_level_shape_unavailable_response_count"],
            1,
        )

    def test_distinct_shape_definition_dimensions_are_independently_locked(
        self,
    ) -> None:
        base = {
            "response_projection": {
                "status": None,
                "incomplete_details": {"reason": "first_reason"},
            },
            "incomplete_details_reason_presence": "reason_present_non_null",
            "output_item_shapes": [
                {"type": None, "status": None},
                {"type": "message", "status": "completed"},
            ],
            "top_level_presence": {
                "status": "present_null",
                "incomplete_details": "present_non_null",
                "stop_sequence": "missing",
            },
        }

        def clone() -> dict[str, object]:
            return json.loads(json.dumps(base))

        base_key = probe._distinct_shape_key(base)
        self.assertIsNotNone(base_key)

        status_missing = clone()
        del status_missing["response_projection"]["status"]
        self.assertNotEqual(probe._distinct_shape_key(status_missing), base_key)

        item_type_missing = clone()
        del item_type_missing["output_item_shapes"][0]["type"]
        self.assertNotEqual(probe._distinct_shape_key(item_type_missing), base_key)

        item_status_missing = clone()
        del item_status_missing["output_item_shapes"][0]["status"]
        self.assertNotEqual(probe._distinct_shape_key(item_status_missing), base_key)

        reordered_items = clone()
        reordered_items["output_item_shapes"].reverse()
        self.assertNotEqual(probe._distinct_shape_key(reordered_items), base_key)

        reordered_presence = clone()
        original_presence = reordered_presence["top_level_presence"]
        reordered_presence["top_level_presence"] = {
            key: original_presence[key]
            for key in reversed(list(original_presence))
        }
        self.assertEqual(probe._distinct_shape_key(reordered_presence), base_key)

        changed_reason_value = clone()
        changed_reason_value["response_projection"]["incomplete_details"] = {
            "reason": "second_reason"
        }
        self.assertEqual(probe._distinct_shape_key(changed_reason_value), base_key)

        for missing_component in (
            "response_projection",
            "output_item_shapes",
            "incomplete_details_reason_presence",
            "top_level_presence",
        ):
            with self.subTest(missing_component=missing_component):
                unavailable = clone()
                del unavailable[missing_component]
                self.assertIsNone(probe._distinct_shape_key(unavailable))
                counts = probe._shape_counts([unavailable])
                self.assertEqual(counts["observed_response_count"], 1)
                self.assertEqual(counts["distinct_top_level_shape_count"], 0)
                self.assertEqual(
                    counts[
                        "distinct_top_level_shape_unavailable_response_count"
                    ],
                    1,
                )

    def test_all_failure_classes_retain_interpretation_guardrails(self) -> None:
        for error_code in (
            "deepseek_responses_client_close_failed",
            "deepseek_responses_http_error",
            "deepseek_responses_sdk_import_failed",
            "deepseek_responses_probe_failed",
        ):
            with self.subTest(error_code=error_code):
                failure = probe._failure_receipt(error_code, outcome_unknown=False)
                self.assertEqual(failure["boundary"], probe._boundary())
                self.assertEqual(failure["transport"], probe._transport())
                self.assertEqual(failure["limitations"], probe._limitations([]))
        for reserved in ("boundary", "transport", "limitations"):
            with self.subTest(reserved=reserved):
                with self.assertRaises(probe.ProbeError) as context:
                    probe._failure_receipt(
                        "synthetic",
                        outcome_unknown=False,
                        **{reserved: {}},
                    )
                self.assertEqual(
                    context.exception.code,
                    "deepseek_responses_failure_guardrail_override",
                )

    def test_shape_only_top_level_diagnostics_never_retain_values(self) -> None:
        cases = [
            ("missing", False, None, None, None),
            ("null", True, None, "present_null", "null"),
            ("empty_object", True, {}, "present_non_null", "object"),
            (
                "object",
                True,
                {"shape-secret-key": "shape-secret-value"},
                "present_non_null",
                "object",
            ),
            ("empty_array", True, [], "present_non_null", "array"),
            (
                "array",
                True,
                ["shape-secret-one", "shape-secret-two"],
                "present_non_null",
                "array",
            ),
            ("empty_string", True, "", "present_non_null", "string"),
            (
                "string",
                True,
                "shape-secret-string",
                "present_non_null",
                "string",
            ),
            ("number", True, 3, "present_non_null", "number"),
            ("boolean", True, True, "present_non_null", "boolean"),
        ]
        for key in probe.SHAPE_ONLY_TOP_LEVEL_KEYS:
            for label, present, value, presence, json_type in cases:
                with self.subTest(key=key, label=label):
                    body = {key: value} if present else {}
                    diagnostic = probe._shape_only_top_level_diagnostic(body, key)
                    expected_count = (
                        len(value) if isinstance(value, (dict, list)) else None
                    )
                    self.assertEqual(
                        diagnostic,
                        {
                            "presence": presence or "missing",
                            "json_type": json_type,
                            "direct_child_count": expected_count,
                        },
                    )
                    serialized = probe._canonical_json(diagnostic)
                    self.assertNotIn("shape-secret", serialized)
                    self.assertNotIn(key, probe._project(body))

        fixed = {
            key: probe._shape_only_top_level_diagnostic({}, key)
            for key in probe.SHAPE_ONLY_TOP_LEVEL_KEYS
        }
        self.assertEqual(set(fixed), {"content_filters", "error", "truncation"})

    def test_output_container_missing_null_type_and_truncation_are_distinct(self) -> None:
        self.assertEqual(
            probe._output_container_diagnostic({}),
            {
                "presence": "missing",
                "json_type": None,
                "item_count": None,
                "shapes_truncated": False,
            },
        )
        self.assertEqual(
            probe._output_container_diagnostic({"output": None})["presence"],
            "present_null",
        )
        self.assertEqual(
            probe._output_container_diagnostic({"output": {}})["json_type"],
            "object",
        )
        self.assertTrue(
            probe._output_container_diagnostic({"output": [{}] * 129})[
                "shapes_truncated"
            ]
        )

    def test_raw_json_and_identifier_handling_fail_closed(self) -> None:
        with self.assertRaises(probe.ProbeError) as duplicate:
            probe._load_raw_body(b'{"status":"completed","status":null}')
        self.assertEqual(
            duplicate.exception.code,
            "deepseek_responses_duplicate_json_key",
        )
        with self.assertRaises(probe.ProbeError) as nonfinite:
            probe._load_raw_body(b'{"usage":{"total_tokens":NaN}}')
        self.assertEqual(
            nonfinite.exception.code,
            "deepseek_responses_nonfinite_json",
        )
        self.assertIsNone(probe._sha256_request_id("unsafe request id with spaces"))
        self.assertIsNone(probe._sha256_request_id("x" * 257))

        timeout = probe._run_error(
            TimeoutError(),
            probe_label="responses_normal_completion",
            client_dispatches=1,
            completed_probes=[],
        )
        self.assertTrue(probe._probe_run_outcome_unknown(timeout))

        class ServerError(RuntimeError):
            status_code = 503
            request_id = None

        server = probe._run_error(
            ServerError(),
            probe_label="responses_normal_completion",
            client_dispatches=1,
            completed_probes=[],
        )
        self.assertTrue(probe._probe_run_outcome_unknown(server))

    def test_missing_key_stops_before_openai_import(self) -> None:
        stderr = io.StringIO()
        original_import = __import__

        def guarded_import(name: str, *args: object, **kwargs: object):
            if name == "openai":
                raise AssertionError("OpenAI SDK import must follow the key gate")
            return original_import(name, *args, **kwargs)

        with patch.dict(os.environ, {}, clear=True), patch(
            "builtins.__import__", side_effect=guarded_import
        ), contextlib.redirect_stderr(stderr):
            result = asyncio.run(probe.main(["--confirm-online"]))
        self.assertEqual(result, 2)
        self.assertEqual(stderr.getvalue().strip(), "DEEPSEEK_API_KEY is not set")

    def test_confirmation_environment_and_debug_gates_precede_sdk_import(self) -> None:
        original_import = __import__

        def guarded_import(name: str, *args: object, **kwargs: object):
            if name == "openai":
                raise AssertionError("OpenAI SDK import must follow every local gate")
            return original_import(name, *args, **kwargs)

        cases = [
            (
                [],
                {"DEEPSEEK_API_KEY": "synthetic-key"},
                "online confirmation is required",
            ),
            (
                ["--confirm-online"],
                {
                    "DEEPSEEK_API_KEY": "synthetic-key",
                    "OPENAI_CUSTOM_HEADERS": "synthetic-header",
                },
                "unsafe OpenAI environment is configured",
            ),
        ]
        for arguments, environment, expected in cases:
            with self.subTest(expected=expected):
                stderr = io.StringIO()
                with patch.dict(os.environ, environment, clear=True), patch(
                    "builtins.__import__", side_effect=guarded_import
                ), contextlib.redirect_stderr(stderr):
                    result = asyncio.run(probe.main(arguments))
                self.assertEqual(result, 2)
                self.assertEqual(stderr.getvalue().strip(), expected)

        logger = logging.getLogger("openai")
        previous_level = logger.level
        try:
            logger.setLevel(logging.DEBUG)
            stderr = io.StringIO()
            with patch.dict(
                os.environ,
                {"DEEPSEEK_API_KEY": "synthetic-key"},
                clear=True,
            ), patch(
                "builtins.__import__", side_effect=guarded_import
            ), contextlib.redirect_stderr(stderr):
                result = asyncio.run(probe.main(["--confirm-online"]))
            self.assertEqual(result, 2)
            self.assertEqual(
                stderr.getvalue().strip(),
                "network debug logging must be disabled",
            )
        finally:
            logger.setLevel(previous_level)

    def test_main_sdk_import_failure_retains_interpretation_guardrails(self) -> None:
        fake_openai = ModuleType("openai")
        stdout = io.StringIO()
        with patch.dict(
            os.environ,
            {"DEEPSEEK_API_KEY": "unit-test-key-must-not-survive"},
            clear=True,
        ), patch.dict(sys.modules, {"openai": fake_openai}), contextlib.redirect_stdout(
            stdout
        ):
            result = asyncio.run(probe.main(["--confirm-online"]))

        self.assertEqual(result, 1)
        failure = json.loads(stdout.getvalue())
        self.assertEqual(
            failure["error_code"],
            "deepseek_responses_sdk_import_failed",
        )
        self.assertEqual(failure["boundary"], probe._boundary())
        self.assertEqual(failure["transport"], probe._transport())
        self.assertEqual(failure["limitations"], probe._limitations([]))
        self.assertNotIn("unit-test-key-must-not-survive", stdout.getvalue())

    def test_main_http_client_construction_failure_retains_guardrails(self) -> None:
        class FailingHTTPClient:
            def __init__(self, **kwargs: object) -> None:
                del kwargs
                raise RuntimeError("http-construction-secret-must-not-survive")

        fake_openai = ModuleType("openai")
        fake_openai.AsyncOpenAI = object
        fake_openai.DefaultAsyncHttpxClient = FailingHTTPClient
        stdout = io.StringIO()
        with patch.dict(
            os.environ,
            {"DEEPSEEK_API_KEY": "unit-test-key-must-not-survive"},
            clear=True,
        ), patch.dict(sys.modules, {"openai": fake_openai}), contextlib.redirect_stdout(
            stdout
        ):
            result = asyncio.run(probe.main(["--confirm-online"]))

        self.assertEqual(result, 1)
        failure = json.loads(stdout.getvalue())
        self.assertEqual(
            failure["error_code"],
            "deepseek_responses_http_client_construction_failed",
        )
        self.assertEqual(failure["boundary"], probe._boundary())
        self.assertEqual(failure["transport"], probe._transport())
        self.assertEqual(failure["limitations"], probe._limitations([]))
        self.assertNotIn("http-construction-secret", stdout.getvalue())
        self.assertNotIn("unit-test-key-must-not-survive", stdout.getvalue())

    def test_main_client_construction_failure_retains_guardrails(self) -> None:
        http_clients: list[object] = []

        class FakeHTTPClient:
            def __init__(self, **kwargs: object) -> None:
                del kwargs
                self.closed = False
                http_clients.append(self)

            async def aclose(self) -> None:
                self.closed = True

        class FailingAsyncOpenAI:
            def __init__(self, **kwargs: object) -> None:
                del kwargs
                raise RuntimeError("client-construction-secret-must-not-survive")

        fake_openai = ModuleType("openai")
        fake_openai.AsyncOpenAI = FailingAsyncOpenAI
        fake_openai.DefaultAsyncHttpxClient = FakeHTTPClient
        stdout = io.StringIO()
        with patch.dict(
            os.environ,
            {"DEEPSEEK_API_KEY": "unit-test-key-must-not-survive"},
            clear=True,
        ), patch.dict(sys.modules, {"openai": fake_openai}), contextlib.redirect_stdout(
            stdout
        ):
            result = asyncio.run(probe.main(["--confirm-online"]))

        self.assertEqual(result, 1)
        failure = json.loads(stdout.getvalue())
        self.assertEqual(
            failure["error_code"],
            "deepseek_responses_client_construction_failed",
        )
        self.assertTrue(failure["http_client_cleanup_succeeded"])
        self.assertNotIn("cleanup_succeeded", failure)
        self.assertEqual(failure["boundary"], probe._boundary())
        self.assertEqual(failure["transport"], probe._transport())
        self.assertEqual(failure["limitations"], probe._limitations([]))
        self.assertEqual(len(http_clients), 1)
        self.assertTrue(http_clients[0].closed)
        self.assertNotIn("client-construction-secret", stdout.getvalue())
        self.assertNotIn("unit-test-key-must-not-survive", stdout.getvalue())

    def test_main_uses_exact_client_controls_and_prints_only_receipt(self) -> None:
        raw_responses = [
            FakeRawResponse(
                {
                    "status": "completed",
                    "incomplete_details": None,
                    "usage": {
                        "input_tokens": 4,
                        "output_tokens": 1,
                        "total_tokens": 5,
                    },
                    "output": [],
                },
                request_id="req-main-normal",
            ),
            FakeRawResponse(
                {
                    "status": "incomplete",
                    "incomplete_details": {"reason": "max_output_tokens"},
                    "usage": {
                        "input_tokens": 8,
                        "output_tokens": 16,
                        "total_tokens": 24,
                    },
                    "output": [],
                },
                request_id="req-main-capped",
            ),
            FakeRawResponse(
                {
                    "status": "incomplete",
                    "incomplete_details": {"reason": "max_output_tokens"},
                    "usage": {
                        "input_tokens": 12,
                        "output_tokens": 96,
                        "total_tokens": 108,
                    },
                    "output": [
                        {"type": "message", "status": "incomplete"}
                    ],
                },
                request_id="req-main-message-stage",
            ),
        ]
        constructor_calls: list[dict[str, object]] = []
        instances: list[object] = []
        http_clients: list[object] = []

        class FakeHTTPClient:
            def __init__(self, **kwargs: object) -> None:
                self.kwargs = dict(kwargs)
                self.closed = False
                http_clients.append(self)

            async def aclose(self) -> None:
                self.closed = True

        class FakeAsyncOpenAI:
            def __init__(self, **kwargs: object) -> None:
                constructor_calls.append(dict(kwargs))
                self.http_client = kwargs["http_client"]
                self.responses = SimpleNamespace(
                    with_raw_response=SimpleNamespace(
                        create=FakeCreate(raw_responses),
                    )
                )
                self.closed = False
                instances.append(self)

            async def close(self) -> None:
                self.closed = True
                await self.http_client.aclose()

        fake_openai = ModuleType("openai")
        fake_openai.AsyncOpenAI = FakeAsyncOpenAI
        fake_openai.DefaultAsyncHttpxClient = FakeHTTPClient
        stdout = io.StringIO()
        stderr = io.StringIO()
        with patch.dict(
            os.environ,
            {"DEEPSEEK_API_KEY": "unit-test-key-must-not-survive"},
            clear=True,
        ), patch.dict(sys.modules, {"openai": fake_openai}), patch(
            "builtins.open", side_effect=AssertionError("file I/O forbidden")
        ), contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            result = asyncio.run(probe.main(["--confirm-online"]))

        self.assertEqual(result, 0)
        self.assertEqual(stderr.getvalue(), "")
        self.assertEqual(len(constructor_calls), 1)
        client_kwargs = constructor_calls[0]
        self.assertEqual(client_kwargs["api_key"], "unit-test-key-must-not-survive")
        self.assertEqual(client_kwargs["base_url"], "https://api.deepseek.com")
        self.assertEqual(client_kwargs["timeout"], 120.0)
        self.assertEqual(client_kwargs["max_retries"], 0)
        self.assertIs(client_kwargs["http_client"], http_clients[0])
        self.assertEqual(
            http_clients[0].kwargs,
            {
                "timeout": 120.0,
                "follow_redirects": False,
                "trust_env": False,
            },
        )
        self.assertTrue(instances[0].closed)
        self.assertTrue(http_clients[0].closed)
        output = stdout.getvalue().strip()
        parsed_output = json.loads(output)
        self.assertEqual(output, probe._canonical_json(parsed_output))
        self.assertEqual(len(parsed_output["probes"]), 3)
        self.assertTrue(parsed_output["post_request_cleanup_succeeded"])
        self.assertTrue(
            parsed_output["limitations"]["message_stage_cap_target_observed"]
        )
        for forbidden in (
            "unit-test-key-must-not-survive",
            "Reply with the single word",
            "Count upward from one",
        ):
            self.assertNotIn(forbidden, output)

    def test_http_failure_receipt_is_bounded_and_does_not_reflect_exception(self) -> None:
        class SyntheticHTTPError(RuntimeError):
            status_code = 429
            request_id = "req-safe-failure"

        class RaisingCreate:
            calls = 0

            async def __call__(self, **kwargs: object) -> object:
                del kwargs
                self.calls += 1
                raise SyntheticHTTPError(
                    "raw-provider-error-and-secret-must-not-survive"
                )

        create = RaisingCreate()

        class FakeHTTPClient:
            def __init__(self, **kwargs: object) -> None:
                del kwargs
                self.closed = False

            async def aclose(self) -> None:
                self.closed = True

        class FakeAsyncOpenAI:
            def __init__(self, **kwargs: object) -> None:
                self.http_client = kwargs["http_client"]
                self.responses = SimpleNamespace(
                    with_raw_response=SimpleNamespace(create=create)
                )

            async def close(self) -> None:
                await self.http_client.aclose()

        fake_openai = ModuleType("openai")
        fake_openai.AsyncOpenAI = FakeAsyncOpenAI
        fake_openai.DefaultAsyncHttpxClient = FakeHTTPClient
        stdout = io.StringIO()
        with patch.dict(
            os.environ,
            {"DEEPSEEK_API_KEY": "unit-test-key-must-not-survive"},
            clear=True,
        ), patch.dict(sys.modules, {"openai": fake_openai}), contextlib.redirect_stdout(
            stdout
        ):
            result = asyncio.run(probe.main(["--confirm-online"]))

        self.assertEqual(result, 1)
        self.assertEqual(create.calls, 1)
        failure = json.loads(stdout.getvalue())
        self.assertEqual(failure["error_code"], "deepseek_responses_http_error")
        self.assertEqual(failure["client_dispatches"], 1)
        self.assertEqual(
            failure["active_raw_response_cleanup_state"],
            "not_started",
        )
        self.assertEqual(failure["http_status"], 429)
        self.assertEqual(
            failure["provider_request_id_sha256"],
            hashlib.sha256(b"req-safe-failure").hexdigest(),
        )
        self.assertFalse(failure["error_body_persisted"])
        self.assertFalse(failure["exception_text_persisted"])
        self.assertFalse(failure["outcome_unknown"])
        self.assertNotIn("raw-provider-error", stdout.getvalue())
        self.assertNotIn("unit-test-key", stdout.getvalue())

    def test_response_processing_failure_retains_observed_transport_metadata(self) -> None:
        raw = FakeRawResponse(
            {
                "status": "unsafe status value must not survive",
                "output": [],
            },
            request_id="req-known-processing-failure",
            status_code=200,
        )

        class FakeHTTPClient:
            def __init__(self, **kwargs: object) -> None:
                del kwargs

            async def aclose(self) -> None:
                return None

        class FakeAsyncOpenAI:
            def __init__(self, **kwargs: object) -> None:
                del kwargs
                self.responses = SimpleNamespace(
                    with_raw_response=SimpleNamespace(
                        create=FakeCreate([raw]),
                    )
                )

            async def close(self) -> None:
                return None

        fake_openai = ModuleType("openai")
        fake_openai.AsyncOpenAI = FakeAsyncOpenAI
        fake_openai.DefaultAsyncHttpxClient = FakeHTTPClient
        stdout = io.StringIO()
        with patch.dict(
            os.environ,
            {"DEEPSEEK_API_KEY": "unit-test-key-must-not-survive"},
            clear=True,
        ), patch.dict(sys.modules, {"openai": fake_openai}), contextlib.redirect_stdout(
            stdout
        ):
            result = asyncio.run(probe.main(["--confirm-online"]))

        self.assertEqual(result, 1)
        failure = json.loads(stdout.getvalue())
        self.assertEqual(
            failure["error_code"],
            "deepseek_responses_schema_value_unsafe",
        )
        self.assertEqual(failure["client_dispatches"], 1)
        self.assertEqual(failure["failed_probe_label"], "responses_normal_completion")
        self.assertEqual(
            failure["active_raw_response_cleanup_state"],
            "succeeded",
        )
        self.assertEqual(failure["http_status"], 200)
        self.assertTrue(failure["provider_request_id_header_present"])
        self.assertEqual(
            failure["provider_request_id_sha256"],
            hashlib.sha256(b"req-known-processing-failure").hexdigest(),
        )
        self.assertFalse(failure["provider_request_id_hash_withheld"])
        self.assertEqual(failure["retained_probe_record_count"], 0)
        self.assertTrue(failure["post_request_cleanup_succeeded"])
        self.assertTrue(raw.closed)
        self.assertNotIn("unsafe status value", stdout.getvalue())
        self.assertNotIn("unit-test-key-must-not-survive", stdout.getvalue())

    def test_second_request_failure_retains_sanitized_completed_prefix(self) -> None:
        first = FakeRawResponse(
            {
                "status": "completed",
                "incomplete_details": None,
                "output": [
                    {
                        "type": "message",
                        "status": "completed",
                        "content": "prefix-content-must-not-survive",
                    }
                ],
            },
            request_id="req-prefix",
        )

        class SecondFailure(RuntimeError):
            status_code = 503
            request_id = "req-second-failure"

        class SecondFailureCreate:
            calls = 0

            async def __call__(self, **kwargs: object) -> object:
                del kwargs
                self.calls += 1
                if self.calls == 1:
                    return first
                raise SecondFailure("second-error-body-must-not-survive")

        create = SecondFailureCreate()
        class FakeHTTPClient:
            def __init__(self, **kwargs: object) -> None:
                del kwargs

            async def aclose(self) -> None:
                return None

        class FakeAsyncOpenAI:
            def __init__(self, **kwargs: object) -> None:
                del kwargs
                self.responses = SimpleNamespace(
                    with_raw_response=SimpleNamespace(create=create)
                )

            async def close(self) -> None:
                return None

        fake_openai = ModuleType("openai")
        fake_openai.AsyncOpenAI = FakeAsyncOpenAI
        fake_openai.DefaultAsyncHttpxClient = FakeHTTPClient
        stdout = io.StringIO()
        with patch.dict(
            os.environ,
            {"DEEPSEEK_API_KEY": "unit-test-key"},
            clear=True,
        ), patch.dict(sys.modules, {"openai": fake_openai}), contextlib.redirect_stdout(
            stdout
        ):
            result = asyncio.run(probe.main(["--confirm-online"]))

        self.assertEqual(result, 1)
        error = json.loads(stdout.getvalue())
        self.assertEqual(error["client_dispatches"], 2)
        self.assertEqual(error["retained_probe_record_count"], 1)
        self.assertEqual(len(error["retained_probe_records"]), 1)
        completed = error["retained_probe_records"][0]
        self.assertEqual(completed["response_projection"]["status"], "completed")
        serialized = probe._canonical_json(error)
        self.assertNotIn("prefix-content-must-not-survive", serialized)
        self.assertNotIn("second-error-body-must-not-survive", serialized)
        self.assertTrue(first.closed)

    def test_third_request_failure_retains_both_completed_prefix_records(self) -> None:
        first = FakeRawResponse(
            {"status": "completed", "incomplete_details": None, "output": []},
            request_id=None,
        )
        second = FakeRawResponse(
            {
                "status": "incomplete",
                "incomplete_details": {"reason": "max_output_tokens"},
                "output": [{"type": "reasoning", "status": "incomplete"}],
            },
            request_id=None,
        )

        class ThirdFailure(RuntimeError):
            status_code = 503
            request_id = None

        class ThirdFailureCreate:
            calls = 0

            async def __call__(self, **kwargs: object) -> object:
                del kwargs
                self.calls += 1
                if self.calls == 1:
                    return first
                if self.calls == 2:
                    return second
                raise ThirdFailure("third-error-body-must-not-survive")

        create = ThirdFailureCreate()
        client = SimpleNamespace(
            responses=SimpleNamespace(
                with_raw_response=SimpleNamespace(create=create),
            )
        )
        with self.assertRaises(probe.ProbeRunError) as context:
            asyncio.run(probe.run_probes(client))
        error = context.exception
        self.assertEqual(error.code, "deepseek_responses_http_error")
        self.assertEqual(error.client_dispatches, 3)
        self.assertEqual(len(error.completed_probes), 2)
        self.assertTrue(first.closed)
        self.assertTrue(second.closed)
        serialized = probe._canonical_json(list(error.completed_probes))
        self.assertNotIn("third-error-body-must-not-survive", serialized)
        failure = probe._failure_receipt(
            error.code,
            outcome_unknown=True,
            retained_probe_records=list(error.completed_probes),
        )
        self.assertTrue(failure["limitations"]["forced_cap_attempt_completed"])
        self.assertFalse(
            failure["limitations"]["message_stage_cap_attempt_completed"]
        )

    def test_second_request_total_timeout_retains_completed_prefix(self) -> None:
        first = FakeRawResponse(
            {"status": "completed", "incomplete_details": None, "output": []},
            request_id=None,
        )

        class HangingSecondCreate:
            calls = 0

            async def __call__(self, **kwargs: object) -> object:
                del kwargs
                self.calls += 1
                if self.calls == 1:
                    return first
                await asyncio.Event().wait()
                raise AssertionError("unreachable")

        create = HangingSecondCreate()

        class FakeHTTPClient:
            def __init__(self, **kwargs: object) -> None:
                del kwargs

            async def aclose(self) -> None:
                return None

        class FakeAsyncOpenAI:
            def __init__(self, **kwargs: object) -> None:
                del kwargs
                self.responses = SimpleNamespace(
                    with_raw_response=SimpleNamespace(create=create)
                )

            async def close(self) -> None:
                return None

        fake_openai = ModuleType("openai")
        fake_openai.AsyncOpenAI = FakeAsyncOpenAI
        fake_openai.DefaultAsyncHttpxClient = FakeHTTPClient
        stdout = io.StringIO()
        with patch.dict(
            os.environ,
            {"DEEPSEEK_API_KEY": "unit-test-key"},
            clear=True,
        ), patch.dict(sys.modules, {"openai": fake_openai}), patch.object(
            probe, "TOTAL_TIMEOUT_SECONDS", 0.01
        ), contextlib.redirect_stdout(stdout):
            result = asyncio.run(probe.main(["--confirm-online"]))

        self.assertEqual(result, 1)
        failure = json.loads(stdout.getvalue())
        self.assertEqual(failure["error_code"], "deepseek_responses_total_timeout")
        self.assertTrue(failure["outcome_unknown"])
        self.assertEqual(failure["client_dispatches"], 2)
        self.assertEqual(
            failure["active_probe_label"],
            "responses_output_cap_attempt",
        )
        self.assertEqual(
            failure["active_raw_response_cleanup_state"],
            "not_started",
        )
        self.assertEqual(failure["retained_probe_record_count"], 1)
        self.assertEqual(len(failure["retained_probe_records"]), 1)
        self.assertEqual(create.calls, 2)
        self.assertTrue(first.closed)

    def test_total_timeout_during_raw_close_retains_current_sanitized_record(
        self,
    ) -> None:
        cases = [
            (2, "responses_output_cap_attempt"),
            (3, probe.MESSAGE_STAGE_PROBE_LABEL),
        ]
        for target_dispatch, expected_label in cases:
            with self.subTest(target_dispatch=target_dispatch):
                responses = [
                    FakeRawResponse(
                        {
                            "status": "completed",
                            "incomplete_details": None,
                            "output": [],
                        },
                        request_id=None,
                    ),
                    FakeRawResponse(
                        {
                            "status": "incomplete",
                            "incomplete_details": {
                                "reason": "max_output_tokens"
                            },
                            "output": [
                                {"type": "reasoning", "status": "incomplete"}
                            ],
                        },
                        request_id=None,
                    ),
                    FakeRawResponse(
                        {
                            "status": "incomplete",
                            "incomplete_details": {
                                "reason": "max_output_tokens"
                            },
                            "output": [
                                {"type": "message", "status": "incomplete"}
                            ],
                        },
                        request_id=None,
                    ),
                ][:target_dispatch]
                close_started = {"value": False}

                async def hang_on_close() -> None:
                    close_started["value"] = True
                    await asyncio.Event().wait()

                responses[-1].http_response = SimpleNamespace(aclose=hang_on_close)

                class FakeHTTPClient:
                    def __init__(self, **kwargs: object) -> None:
                        del kwargs

                    async def aclose(self) -> None:
                        return None

                class FakeAsyncOpenAI:
                    def __init__(self, **kwargs: object) -> None:
                        del kwargs
                        self.responses = SimpleNamespace(
                            with_raw_response=SimpleNamespace(
                                create=FakeCreate(responses),
                            )
                        )

                    async def close(self) -> None:
                        return None

                fake_openai = ModuleType("openai")
                fake_openai.AsyncOpenAI = FakeAsyncOpenAI
                fake_openai.DefaultAsyncHttpxClient = FakeHTTPClient
                stdout = io.StringIO()
                with patch.dict(
                    os.environ,
                    {"DEEPSEEK_API_KEY": "unit-test-key-must-not-survive"},
                    clear=True,
                ), patch.dict(
                    sys.modules,
                    {"openai": fake_openai},
                ), patch.object(
                    probe,
                    "TOTAL_TIMEOUT_SECONDS",
                    0.01,
                ), contextlib.redirect_stdout(stdout):
                    result = asyncio.run(probe.main(["--confirm-online"]))

                self.assertEqual(result, 1)
                self.assertTrue(close_started["value"])
                failure = json.loads(stdout.getvalue())
                self.assertEqual(
                    failure["error_code"],
                    "deepseek_responses_total_timeout",
                )
                self.assertTrue(failure["outcome_unknown"])
                self.assertEqual(failure["client_dispatches"], target_dispatch)
                self.assertEqual(failure["active_probe_label"], expected_label)
                self.assertEqual(
                    failure["active_raw_response_cleanup_state"],
                    "cancelled",
                )
                self.assertEqual(
                    failure["retained_probe_record_count"],
                    target_dispatch,
                )
                self.assertEqual(
                    len(failure["retained_probe_records"]),
                    target_dispatch,
                )
                self.assertEqual(
                    failure["retained_probe_records"][-1]["response_projection"][
                        "status"
                    ],
                    "incomplete",
                )
                self.assertEqual(
                    failure["retained_probe_records"][-1][
                        "raw_response_cleanup_state"
                    ],
                    "cancelled",
                )
                self.assertTrue(failure["post_request_cleanup_succeeded"])
                self.assertNotIn(
                    "unit-test-key-must-not-survive",
                    stdout.getvalue(),
                )

    def test_raw_cleanup_failure_retains_the_sanitized_current_record(self) -> None:
        raw = FakeRawResponse(
            {"status": "completed", "incomplete_details": None, "output": []},
            request_id=None,
        )

        async def fail_close() -> None:
            raise RuntimeError("raw-close-secret-must-not-survive")

        raw.http_response = SimpleNamespace(aclose=fail_close)
        client, _ = _fake_client([raw])
        with self.assertRaises(probe.ProbeRunError) as context:
            asyncio.run(probe.run_probes(client))
        error = context.exception
        self.assertEqual(error.code, "deepseek_responses_raw_close_failed")
        self.assertEqual(len(error.completed_probes), 1)
        self.assertEqual(
            error.completed_probes[0]["response_projection"]["status"],
            "completed",
        )
        self.assertNotIn(
            "raw-close-secret",
            probe._canonical_json(list(error.completed_probes)),
        )

    def test_main_raw_cleanup_failure_retains_guardrails_and_probe_data(self) -> None:
        raw = FakeRawResponse(
            {
                "status": "completed",
                "incomplete_details": None,
                "output": [
                    {
                        "type": "message",
                        "status": "completed",
                        "content": "raw-content-must-not-survive",
                    }
                ],
            },
            request_id="req-raw-close-main",
        )

        async def fail_raw_close() -> None:
            raise RuntimeError("raw-close-secret-must-not-survive")

        raw.http_response = SimpleNamespace(aclose=fail_raw_close)

        class FakeHTTPClient:
            def __init__(self, **kwargs: object) -> None:
                del kwargs

            async def aclose(self) -> None:
                return None

        class FakeAsyncOpenAI:
            def __init__(self, **kwargs: object) -> None:
                del kwargs
                self.responses = SimpleNamespace(
                    with_raw_response=SimpleNamespace(
                        create=FakeCreate([raw]),
                    )
                )

            async def close(self) -> None:
                return None

        fake_openai = ModuleType("openai")
        fake_openai.AsyncOpenAI = FakeAsyncOpenAI
        fake_openai.DefaultAsyncHttpxClient = FakeHTTPClient
        stdout = io.StringIO()
        with patch.dict(
            os.environ,
            {"DEEPSEEK_API_KEY": "unit-test-key-must-not-survive"},
            clear=True,
        ), patch.dict(sys.modules, {"openai": fake_openai}), contextlib.redirect_stdout(
            stdout
        ):
            result = asyncio.run(probe.main(["--confirm-online"]))

        self.assertEqual(result, 1)
        failure = json.loads(stdout.getvalue())
        self.assertEqual(
            failure["error_code"],
            "deepseek_responses_raw_close_failed",
        )
        self.assertEqual(
            failure["active_raw_response_cleanup_state"],
            "failed",
        )
        self.assertEqual(failure["http_status"], 200)
        self.assertTrue(failure["provider_request_id_header_present"])
        self.assertEqual(
            failure["provider_request_id_sha256"],
            hashlib.sha256(b"req-raw-close-main").hexdigest(),
        )
        self.assertFalse(failure["provider_request_id_hash_withheld"])
        self.assertEqual(failure["retained_probe_record_count"], 1)
        self.assertEqual(len(failure["retained_probe_records"]), 1)
        self.assertEqual(
            failure["retained_probe_records"][0]["response_projection"]["status"],
            "completed",
        )
        self.assertEqual(failure["retained_probe_records"][0]["http_status"], 200)
        self.assertTrue(
            failure["retained_probe_records"][0][
                "provider_request_id_header_present"
            ]
        )
        self.assertEqual(
            failure["retained_probe_records"][0]["raw_response_cleanup_state"],
            "failed",
        )
        self.assertTrue(failure["post_request_cleanup_succeeded"])
        self.assertNotIn("cleanup_succeeded", failure)
        self.assertEqual(failure["boundary"], probe._boundary())
        self.assertEqual(failure["transport"], probe._transport())
        self.assertEqual(
            failure["limitations"],
            probe._limitations(failure["retained_probe_records"]),
        )
        self.assertNotIn("raw-content-must-not-survive", stdout.getvalue())
        self.assertNotIn("raw-close-secret-must-not-survive", stdout.getvalue())
        self.assertNotIn("unit-test-key-must-not-survive", stdout.getvalue())

    def test_successful_probe_close_failure_is_normalized(self) -> None:
        responses = [
            FakeRawResponse(
                {"status": "completed", "incomplete_details": None, "output": []},
                request_id=None,
            ),
            FakeRawResponse(
                {
                    "status": "incomplete",
                    "incomplete_details": {"reason": "max_output_tokens"},
                    "output": [],
                },
                request_id=None,
            ),
            FakeRawResponse(
                {
                    "status": "incomplete",
                    "incomplete_details": {"reason": "max_output_tokens"},
                    "output": [
                        {"type": "message", "status": "incomplete"}
                    ],
                },
                request_id=None,
            ),
        ]

        class FakeHTTPClient:
            def __init__(self, **kwargs: object) -> None:
                del kwargs

            async def aclose(self) -> None:
                return None

        class FakeAsyncOpenAI:
            def __init__(self, **kwargs: object) -> None:
                del kwargs
                self.responses = SimpleNamespace(
                    with_raw_response=SimpleNamespace(create=FakeCreate(responses))
                )

            async def close(self) -> None:
                raise RuntimeError("close-secret-must-not-survive")

        fake_openai = ModuleType("openai")
        fake_openai.AsyncOpenAI = FakeAsyncOpenAI
        fake_openai.DefaultAsyncHttpxClient = FakeHTTPClient
        stdout = io.StringIO()
        with patch.dict(
            os.environ,
            {"DEEPSEEK_API_KEY": "unit-test-key"},
            clear=True,
        ), patch.dict(sys.modules, {"openai": fake_openai}), contextlib.redirect_stdout(
            stdout
        ):
            result = asyncio.run(probe.main(["--confirm-online"]))
        self.assertEqual(result, 1)
        failure = json.loads(stdout.getvalue())
        self.assertEqual(
            failure["error_code"],
            "deepseek_responses_client_close_failed",
        )
        self.assertFalse(failure["post_request_cleanup_succeeded"])
        self.assertNotIn("cleanup_succeeded", failure)
        self.assertEqual(failure["retained_probe_record_count"], 3)
        self.assertEqual(len(failure["retained_probe_records"]), 3)
        self.assertEqual(failure["boundary"], probe._boundary())
        self.assertEqual(failure["transport"], probe._transport())
        self.assertEqual(
            failure["limitations"],
            probe._limitations(failure["retained_probe_records"]),
        )
        self.assertTrue(
            failure["limitations"]["message_stage_cap_target_observed"]
        )
        self.assertNotIn("close-secret", stdout.getvalue())

    def test_primary_request_failure_survives_cleanup_failure(self) -> None:
        class SyntheticHTTPError(RuntimeError):
            status_code = 503
            request_id = None

        class RaisingCreate:
            async def __call__(self, **kwargs: object) -> object:
                del kwargs
                raise SyntheticHTTPError("primary-secret-must-not-survive")

        class FailingHTTPClient:
            def __init__(self, **kwargs: object) -> None:
                del kwargs

            async def aclose(self) -> None:
                raise RuntimeError("http-close-secret-must-not-survive")

        class FailingClient:
            def __init__(self, **kwargs: object) -> None:
                del kwargs
                self.responses = SimpleNamespace(
                    with_raw_response=SimpleNamespace(create=RaisingCreate())
                )

            async def close(self) -> None:
                raise RuntimeError("client-close-secret-must-not-survive")

        fake_openai = ModuleType("openai")
        fake_openai.AsyncOpenAI = FailingClient
        fake_openai.DefaultAsyncHttpxClient = FailingHTTPClient
        stdout = io.StringIO()
        with patch.dict(
            os.environ,
            {"DEEPSEEK_API_KEY": "unit-test-key"},
            clear=True,
        ), patch.dict(sys.modules, {"openai": fake_openai}), contextlib.redirect_stdout(
            stdout
        ):
            result = asyncio.run(probe.main(["--confirm-online"]))
        self.assertEqual(result, 1)
        failure = json.loads(stdout.getvalue())
        self.assertEqual(failure["error_code"], "deepseek_responses_http_error")
        self.assertTrue(failure["outcome_unknown"])
        self.assertFalse(failure["post_request_cleanup_succeeded"])
        self.assertNotIn("cleanup_succeeded", failure)
        for forbidden in ("primary-secret", "client-close-secret", "http-close-secret"):
            self.assertNotIn(forbidden, stdout.getvalue())

    def test_cancellation_propagates_and_cleanup_is_attempted(self) -> None:
        class CancelCreate:
            calls = 0

            async def __call__(self, **kwargs: object) -> object:
                del kwargs
                self.calls += 1
                raise asyncio.CancelledError

        create = CancelCreate()
        cleanup = {"client": 0, "http": 0}

        class FakeHTTPClient:
            def __init__(self, **kwargs: object) -> None:
                del kwargs

            async def aclose(self) -> None:
                cleanup["http"] += 1

        class FakeAsyncOpenAI:
            def __init__(self, **kwargs: object) -> None:
                self.http_client = kwargs["http_client"]
                self.responses = SimpleNamespace(
                    with_raw_response=SimpleNamespace(create=create)
                )

            async def close(self) -> None:
                cleanup["client"] += 1

        fake_openai = ModuleType("openai")
        fake_openai.AsyncOpenAI = FakeAsyncOpenAI
        fake_openai.DefaultAsyncHttpxClient = FakeHTTPClient
        with patch.dict(
            os.environ,
            {"DEEPSEEK_API_KEY": "unit-test-key"},
            clear=True,
        ), patch.dict(sys.modules, {"openai": fake_openai}):
            with self.assertRaises(asyncio.CancelledError):
                asyncio.run(probe.main(["--confirm-online"]))
        self.assertEqual(create.calls, 1)
        self.assertEqual(cleanup, {"client": 1, "http": 1})

    def test_script_has_no_researchops_or_adapter_import(self) -> None:
        source = SCRIPT_PATH.read_text(encoding="utf-8")
        tree = ast.parse(source)
        imported: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module)
        self.assertFalse(
            any(
                name == "researchops" or name.startswith("researchops.")
                for name in imported
            )
        )
        self.assertFalse(any("adapter" in name for name in imported))
        self.assertIn("client.responses.with_raw_response.create", source)
        self.assertIn("await raw.http_response.aclose()", source)
        self.assertNotIn("raw.parse(", source)
        self.assertNotIn("model_dump(", source)

    def test_locked_sdk_uses_legacy_raw_wrapper_without_direct_close(self) -> None:
        import importlib.metadata

        import httpx2
        from openai import APITimeoutError
        from openai._legacy_response import LegacyAPIResponse

        self.assertEqual(importlib.metadata.version("openai"), "3.1.0")
        self.assertTrue(probe._openai_sdk_version_is_exact())
        self.assertFalse(hasattr(LegacyAPIResponse, "close"))
        self.assertTrue(hasattr(LegacyAPIResponse, "content"))
        timeout = probe._run_error(
            APITimeoutError(request=httpx2.Request("POST", "https://example.invalid")),
            probe_label="responses_normal_completion",
            client_dispatches=1,
            completed_probes=[],
        )
        self.assertEqual(timeout.code, "deepseek_responses_request_timeout")
        self.assertTrue(probe._probe_run_outcome_unknown(timeout))

    def test_real_sdk_raw_wrapper_closes_async_mock_responses(self) -> None:
        import httpx2
        from openai import AsyncOpenAI, DefaultAsyncHttpxClient

        response_bodies = [
            {
                "status": "completed",
                "incomplete_details": None,
                "usage": {"input_tokens": 4, "output_tokens": 1, "total_tokens": 5},
                "output": [],
            },
            {
                "status": "incomplete",
                "incomplete_details": {"reason": "max_output_tokens"},
                "usage": {"input_tokens": 8, "output_tokens": 16, "total_tokens": 24},
                "output": [],
            },
            {
                "status": "incomplete",
                "incomplete_details": {"reason": "max_output_tokens"},
                "usage": {"input_tokens": 12, "output_tokens": 96, "total_tokens": 108},
                "output": [{"type": "message", "status": "incomplete"}],
            },
        ]
        requests: list[httpx2.Request] = []
        responses: list[httpx2.Response] = []

        async def handler(request: httpx2.Request) -> httpx2.Response:
            requests.append(request)
            response = httpx2.Response(
                200,
                headers={"x-request-id": f"req-mock-{len(requests)}"},
                json=response_bodies[len(requests) - 1],
            )
            responses.append(response)
            return response

        async def exercise() -> dict[str, object]:
            http_client = DefaultAsyncHttpxClient(
                transport=httpx2.MockTransport(handler),
                timeout=120.0,
                follow_redirects=False,
                trust_env=False,
            )
            client = AsyncOpenAI(
                api_key="synthetic-sdk-key",
                base_url="https://api.deepseek.com",
                timeout=120.0,
                max_retries=0,
                http_client=http_client,
            )
            try:
                return await probe.run_probes(client)
            finally:
                await client.close()
                await http_client.aclose()

        with patch.dict(os.environ, {}, clear=True):
            receipt = asyncio.run(exercise())

        self.assertEqual(len(requests), 3)
        self.assertTrue(all(request.method == "POST" for request in requests))
        self.assertTrue(all(request.url.path == "/responses" for request in requests))
        self.assertTrue(all(response.is_closed for response in responses))
        self.assertEqual(receipt["status"], "completed")
        self.assertEqual(
            [record["response_projection"]["status"] for record in receipt["probes"]],
            ["completed", "incomplete", "incomplete"],
        )

if __name__ == "__main__":
    unittest.main()
