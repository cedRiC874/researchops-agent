from __future__ import annotations

import copy
import json
import unittest
from pathlib import Path
from typing import Any, Mapping

from jsonschema import Draft202012Validator


ROOT = Path(__file__).resolve().parents[1]
PACKAGE_ROOT = ROOT / "evals" / "provider_completion_telemetry_v1"
CONTRACT_PATH = PACKAGE_ROOT / "provider_completion_record_contract_v1.json"
SCHEMA_PATH = PACKAGE_ROOT / "schemas" / "provider_completion_record_v1.schema.json"


def _scalar(
    availability: str,
    value: str | None = None,
    *,
    redacted: bool = False,
    truncated: bool = False,
) -> dict[str, Any]:
    return {
        "availability": availability,
        "value": value,
        "redaction_applied": redacted,
        "truncated": truncated,
    }


def _details(
    availability: str,
    value: Mapping[str, Any] | None = None,
    *,
    redacted: bool = False,
    truncated: bool = False,
    omitted: int = 0,
) -> dict[str, Any]:
    return {
        "availability": availability,
        "value": dict(value) if value is not None else None,
        "redaction_applied": redacted,
        "truncated": truncated,
        "omitted_child_field_count": omitted,
    }


def _availability_value(availability: str, value: Any) -> dict[str, Any]:
    return {"availability": availability, "value": value}


def _normalized_usage(**overrides: int | None) -> dict[str, int | None]:
    value: dict[str, int | None] = {
        "requests": 1,
        "input_tokens": 10,
        "output_tokens": 5,
        "total_tokens": 15,
        "cached_input_tokens": 0,
        "cache_write_tokens": None,
        "reasoning_tokens": None,
    }
    value.update(overrides)
    return value


def _usage(
    availability: str = "provided",
    *,
    normalized: Mapping[str, int | None] | None = None,
    counters: list[dict[str, Any]] | None = None,
    complete: bool = True,
) -> dict[str, Any]:
    if availability == "provided":
        normalized_value = dict(normalized or _normalized_usage())
        counter_value = counters or [
            {"path": "input_tokens", "value": normalized_value["input_tokens"]},
            {"path": "output_tokens", "value": normalized_value["output_tokens"]},
            {"path": "total_tokens", "value": normalized_value["total_tokens"]},
        ]
    else:
        normalized_value = _normalized_usage(
            requests=None,
            input_tokens=None,
            output_tokens=None,
            total_tokens=None,
            cached_input_tokens=None,
            cache_write_tokens=None,
            reasoning_tokens=None,
        )
        counter_value = []
        complete = False
    return {
        "availability": availability,
        "native_value_state": (
            "object" if availability == "provided" else availability
        ),
        "complete": complete,
        "normalized": normalized_value,
        "native_numeric_counters": counter_value,
        "omitted_non_numeric_field_count": 0,
        "truncated": False,
    }


def _completed_record() -> dict[str, Any]:
    return {
        "telemetry_schema_version": "provider-completion-record/1.0",
        "telemetry_schema_sha256": "c" * 64,
        "record_provenance": "offline_validation",
        "adapter_version": "openai-responses-adapter/1.0",
        "mapping_schema_version": "provider-completion-mapping/2.0",
        "mapping_version": "openai-responses-v1",
        "mapping_sha256": "a" * 64,
        "provider_id": "openai",
        "api_surface": "responses",
        "transport_id": "openai_responses",
        "response_index": 0,
        "request_index": 0,
        "native_status": _scalar("provided", "completed"),
        "native_finish_reason": _scalar("not_provided"),
        "native_stop_reason": _scalar("not_provided"),
        "native_stop_sequence": _scalar("not_provided"),
        "native_incomplete_details": _details("provided"),
        "normalized_completion_state": "completed",
        "truncation_signal_source": "native_status",
        "matched_rule_id": "openai-resp-recognized-001",
        "provider_request_id_sha256": _availability_value("provided", "b" * 64),
        "http_status": _availability_value("provided", 200),
        "usage": _usage(),
        "output_token_cap": _availability_value("provided", 256),
        "output_counter_comparability": "comparable",
        "output_counter_path": "output_tokens",
    }


def _semantic_errors(record: Mapping[str, Any], contract: Mapping[str, Any]) -> list[str]:
    """Exercise T1 invariants that Draft 2020-12 cannot express by itself."""

    errors: list[str] = []
    binding = contract["mapping_binding"]
    if binding["key_fields"] != ["provider_id", "api_surface", "transport_id"]:
        errors.append("mapping_key")

    usage = record["usage"]
    paths = [item["path"] for item in usage["native_numeric_counters"]]
    if paths != sorted(paths) or len(paths) != len(set(paths)):
        errors.append("usage_counter_paths")

    if record["output_counter_comparability"] == "comparable":
        counter_path = record["output_counter_path"]
        counters = {
            item["path"]: item["value"]
            for item in usage["native_numeric_counters"]
        }
        if counter_path not in counters or counters[counter_path] is None:
            errors.append("comparable_counter_missing")

    if record["truncation_signal_source"] == "token_cap_fallback":
        cap = record["output_token_cap"]["value"]
        counter_path = record["output_counter_path"]
        counters = {
            item["path"]: item["value"]
            for item in usage["native_numeric_counters"]
        }
        if counters.get(counter_path) != cap:
            errors.append("fallback_not_exact")

    limits = contract["field_limits"]
    scalar_limits = {
        "native_status": limits["native_status_utf8_bytes_max"],
        "native_finish_reason": limits["native_finish_reason_utf8_bytes_max"],
        "native_stop_reason": limits["native_stop_reason_utf8_bytes_max"],
        "native_stop_sequence": limits["native_stop_sequence_utf8_bytes_max"],
    }
    for field, limit in scalar_limits.items():
        value = record[field]["value"]
        if isinstance(value, str) and len(value.encode("utf-8")) > limit:
            errors.append(f"{field}_utf8_limit")

    details = record["native_incomplete_details"]["value"]
    if details is not None:
        encoded = json.dumps(
            details,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        if len(encoded) > limits["native_incomplete_details_canonical_json_utf8_bytes_max"]:
            errors.append("incomplete_details_utf8_limit")

    record_bytes = json.dumps(
        record,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    if len(record_bytes) > limits["record_canonical_json_utf8_bytes_max"]:
        errors.append("record_utf8_limit")
    return errors


class ProviderCompletionRecordContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.contract = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))
        cls.schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
        Draft202012Validator.check_schema(cls.schema)
        cls.validator = Draft202012Validator(cls.schema)

    def assertValid(self, value: Mapping[str, Any]) -> None:
        errors = sorted(self.validator.iter_errors(value), key=lambda item: list(item.path))
        self.assertEqual([error.message for error in errors], [])
        self.assertEqual(_semantic_errors(value, self.contract), [])

    def assertSchemaInvalid(self, value: Mapping[str, Any]) -> None:
        self.assertTrue(list(self.validator.iter_errors(value)))

    def test_contract_fixes_event_version_surface_key_and_write_pipeline(self) -> None:
        self.assertEqual(
            self.contract["contract_schema_version"],
            "provider-completion-record-contract/1.0",
        )
        self.assertEqual(
            self.contract["event_type"], "model_response_telemetry_recorded"
        )
        self.assertEqual(
            self.contract["record_schema_version"],
            "provider-completion-record/1.0",
        )
        self.assertEqual(
            self.contract["mapping_binding"]["key_fields"],
            ["provider_id", "api_surface", "transport_id"],
        )
        self.assertEqual(
            self.contract["mapping_binding"]["surface_mismatch_behavior"],
            "fail_closed",
        )
        self.assertEqual(
            self.contract["write_pipeline"],
            [
                "adapter_observes_response_metadata_without_retaining_body",
                "project_exact_allowlist_and_preserve_key_presence",
                "hash_provider_request_id_in_memory_and_discard_raw_identifier",
                "sanitize_and_bound_free_text_before_record_construction",
                "select_mapping_by_provider_id_api_surface_and_transport_id_or_fail_closed",
                "map_the_sanitized_projection_without_nearest_enum_coercion",
                "validate_record_schema_and_cross_field_semantics",
                "append_completion_metadata_and_usage_as_one_event",
                "verify_append_only_event_chain_before_artifact_release",
            ],
        )
        self.assertFalse(self.contract["adapter_changes_included"])
        self.assertFalse(self.contract["event_chain_changes_included"])
        self.assertFalse(self.contract["online_calls_performed"])

    def test_complete_record_validates(self) -> None:
        self.assertValid(_completed_record())

    def test_absent_members_and_extra_members_are_not_missing_semantics(self) -> None:
        missing = _completed_record()
        missing.pop("native_status")
        self.assertSchemaInvalid(missing)

        extra = _completed_record()
        extra["provider_response_body"] = "forbidden"
        self.assertSchemaInvalid(extra)

        nested_extra = _completed_record()
        nested_extra["native_status"]["raw_value"] = "completed"
        self.assertSchemaInvalid(nested_extra)

        details_extra = _completed_record()
        details_extra["native_incomplete_details"] = _details(
            "provided", {"reason": None, "provider_echo": "forbidden"}
        )
        self.assertSchemaInvalid(details_extra)

    def test_explicit_null_and_not_provided_are_distinct_valid_records(self) -> None:
        explicit_null = _completed_record()
        explicit_null.update(
            {
                "native_status": _scalar("provided", None),
                "native_finish_reason": _scalar("not_provided"),
                "native_stop_reason": _scalar("not_provided"),
                "native_stop_sequence": _scalar("not_provided"),
                "native_incomplete_details": _details("provided"),
                "normalized_completion_state": "not_provided",
                "truncation_signal_source": "none",
                "matched_rule_id": "openai-resp-absent-null-001",
            }
        )
        self.assertValid(explicit_null)

        absent = copy.deepcopy(explicit_null)
        absent["native_status"] = _scalar("not_provided")
        absent["native_incomplete_details"] = _details("not_provided")
        absent["matched_rule_id"] = "openai-resp-absent-001"
        self.assertValid(absent)
        self.assertNotEqual(explicit_null["native_status"], absent["native_status"])

    def test_historical_not_persisted_is_explicit_and_cannot_mix_with_current_data(self) -> None:
        historical = _completed_record()
        historical.update(
            {
                "record_provenance": "historical_projection",
                "native_status": _scalar("not_persisted"),
                "native_finish_reason": _scalar("not_persisted"),
                "native_stop_reason": _scalar("not_persisted"),
                "native_stop_sequence": _scalar("not_persisted"),
                "native_incomplete_details": _details("not_persisted"),
                "normalized_completion_state": "not_persisted",
                "truncation_signal_source": "none",
                "matched_rule_id": "legacy-not-persisted-v1",
                "provider_request_id_sha256": _availability_value(
                    "not_persisted", None
                ),
                "http_status": _availability_value("not_persisted", None),
                "usage": _usage("not_persisted"),
                "output_token_cap": _availability_value("not_persisted", None),
                "output_counter_comparability": "not_persisted",
                "output_counter_path": None,
            }
        )
        self.assertValid(historical)

        mixed = copy.deepcopy(historical)
        mixed["native_status"] = _scalar("provided", "completed")
        self.assertSchemaInvalid(mixed)
        self.assertFalse(
            self.contract["missing_semantics"]["new_adapter_may_emit_not_persisted"]
        )

    def test_unknown_native_value_is_preserved_as_unmapped(self) -> None:
        unknown = _completed_record()
        unknown.update(
            {
                "native_status": _scalar("provided", "future_status"),
                "normalized_completion_state": "unmapped",
                "truncation_signal_source": "native_status",
                "matched_rule_id": "openai-resp-unknown-001",
            }
        )
        self.assertValid(unknown)

        vacuous = copy.deepcopy(unknown)
        vacuous["native_status"] = _scalar("not_provided")
        vacuous["native_incomplete_details"] = _details("not_provided")
        self.assertSchemaInvalid(vacuous)

    def test_state_source_and_native_discriminator_invariants_fail_closed(self) -> None:
        wrong_source = _completed_record()
        wrong_source["truncation_signal_source"] = "none"
        self.assertSchemaInvalid(wrong_source)

        no_discriminator = _completed_record()
        no_discriminator["native_status"] = _scalar("provided", None)
        self.assertSchemaInvalid(no_discriminator)

        not_provided_with_value = _completed_record()
        not_provided_with_value.update(
            {
                "normalized_completion_state": "not_provided",
                "truncation_signal_source": "none",
                "native_status": _scalar("provided", "completed"),
            }
        )
        self.assertSchemaInvalid(not_provided_with_value)

    def test_token_cap_fallback_requires_absent_native_and_exact_counter(self) -> None:
        fallback = _completed_record()
        fallback.update(
            {
                "native_status": _scalar("not_provided"),
                "native_finish_reason": _scalar("not_provided"),
                "native_stop_reason": _scalar("not_provided"),
                "native_stop_sequence": _scalar("not_provided"),
                "native_incomplete_details": _details("not_provided"),
                "normalized_completion_state": "incomplete_length",
                "truncation_signal_source": "token_cap_fallback",
                "matched_rule_id": "runtime-token-cap-fallback-v1",
                "usage": _usage(
                    normalized=_normalized_usage(output_tokens=16, total_tokens=26),
                    counters=[
                        {"path": "input_tokens", "value": 10},
                        {"path": "output_tokens", "value": 16},
                        {"path": "total_tokens", "value": 26},
                    ],
                ),
                "output_token_cap": _availability_value("provided", 16),
            }
        )
        self.assertValid(fallback)

        native_present = copy.deepcopy(fallback)
        native_present["native_status"] = _scalar("provided", "incomplete")
        self.assertSchemaInvalid(native_present)

        mismatch = copy.deepcopy(fallback)
        mismatch["output_token_cap"]["value"] = 17
        self.assertEqual(_semantic_errors(mismatch, self.contract), ["fallback_not_exact"])

    def test_usage_paths_must_be_sorted_unique_and_numeric_only(self) -> None:
        unsorted = _completed_record()
        unsorted["usage"]["native_numeric_counters"].reverse()
        self.assertEqual(_semantic_errors(unsorted, self.contract), ["usage_counter_paths"])

        non_numeric = _completed_record()
        non_numeric["usage"]["native_numeric_counters"][0]["value"] = "ten"
        self.assertSchemaInvalid(non_numeric)

        boolean_counter = _completed_record()
        boolean_counter["usage"]["native_numeric_counters"][0]["value"] = True
        self.assertSchemaInvalid(boolean_counter)

    def test_versions_enums_and_hashes_are_strict(self) -> None:
        mutations = []
        wrong_version = _completed_record()
        wrong_version["telemetry_schema_version"] = "provider-completion-record/2.0"
        mutations.append(wrong_version)

        wrong_state = _completed_record()
        wrong_state["normalized_completion_state"] = "almost_completed"
        mutations.append(wrong_state)

        wrong_source = _completed_record()
        wrong_source["truncation_signal_source"] = "provider_guess"
        mutations.append(wrong_source)

        wrong_hash = _completed_record()
        wrong_hash["mapping_sha256"] = "A" * 64
        mutations.append(wrong_hash)

        raw_request_id = _completed_record()
        raw_request_id["provider_request_id"] = "req_private"
        mutations.append(raw_request_id)

        for index, mutation in enumerate(mutations):
            with self.subTest(index=index):
                self.assertSchemaInvalid(mutation)

    def test_truncation_markers_and_declared_byte_limits_are_explicit(self) -> None:
        truncated = _completed_record()
        truncated["native_stop_sequence"] = _scalar(
            "provided", "prefix[TRUNCATED]", truncated=True
        )
        self.assertValid(truncated)

        missing_marker = copy.deepcopy(truncated)
        missing_marker["native_stop_sequence"]["value"] = "prefix"
        self.assertSchemaInvalid(missing_marker)

        details = _completed_record()
        details["native_incomplete_details"] = _details(
            "provided",
            {"reason": "max_output_tokens", "_telemetry_truncated": True},
            truncated=True,
            omitted=2,
        )
        self.assertValid(details)

        limits = self.contract["field_limits"]
        self.assertEqual(limits["native_stop_sequence_utf8_bytes_max"], 64)
        self.assertEqual(
            limits["native_incomplete_details_canonical_json_utf8_bytes_max"], 512
        )
        self.assertEqual(limits["usage_canonical_json_utf8_bytes_max"], 4096)
        self.assertTrue(limits["utf8_byte_limits_require_runtime_validation"])

    def test_privacy_closure_and_historical_boundaries_are_machine_declared(self) -> None:
        forbidden = set(self.contract["forbidden_inputs"])
        self.assertTrue(
            {
                "message_content",
                "tool_call_arguments",
                "system_prompt",
                "raw_response_body",
                "raw_headers",
                "api_key",
                "authorization_header",
                "traceback",
                "absolute_path",
            }
            <= forbidden
        )
        closing = self.contract["closing_gate"]
        self.assertFalse(closing["green_ci_alone_satisfies_gate"])
        self.assertTrue(closing["hardcoded_historical_response_count_forbidden"])
        self.assertIn(
            "persisted_response_count_equals_the_derived_observed_response_count",
            closing["preconditions"],
        )
        self.assertIn(
            "exact_response_count_is_not_preregistered",
            closing["preconditions"],
        )
        self.assertIn(
            "every_attempt_terminal_kind_equals_response_accepted",
            closing["preconditions"],
        )
        denominator = self.contract["runtime_denominator_contract"]
        self.assertFalse(denominator["exact_response_count_preregistered"])
        self.assertEqual(
            denominator["denominator_algorithm"],
            "transport-response-finalization-v1",
        )
        self.assertTrue(denominator["seal_requires_no_pending_attempts"])
        self.assertFalse(denominator["seal_requires_reaching_request_or_turn_cap"])
        self.assertIn(
            "every_truncation_signal_source_equals_native_status",
            closing["preconditions"],
        )
        historical = self.contract["historical_boundaries"]
        self.assertFalse(historical["retroactive_kimi_attribution_recovered"])
        self.assertFalse(historical["retroactive_depth60_attribution_recovered"])
        self.assertFalse(historical["seen_task_rerun_authorized"])
        self.assertEqual(historical["locked_depth60_score"], "20/60")
        self.assertTrue(
            historical[
                "new_accuracy_number_requires_preregistered_replacement_"
                "evaluator_and_new_unseen_tasks"
            ]
        )

    def test_runtime_authority_offline_provenance_and_artifact_denominator_are_fixed(self) -> None:
        runtime = self.contract["runtime_implementation"]
        self.assertTrue(
            runtime["live_builder_and_validator_require_verified_runtime_completion_binding"]
        )
        self.assertTrue(
            runtime["live_artifact_validator_requires_verified_capture_plan_binding"]
        )
        self.assertFalse(runtime["live_builder_accepts_arbitrary_mapping_resolver"])
        self.assertFalse(runtime["live_builder_accepts_caller_output_counter_semantics"])
        self.assertEqual(runtime["offline_builder_record_provenance"], "offline_validation")
        self.assertFalse(runtime["offline_builder_records_are_closure_eligible"])
        self.assertFalse(
            self.contract["offline_validation_provenance_is_closure_eligible"]
        )

        artifact = self.contract["artifact_envelope"]
        self.assertEqual(
            artifact["exact_fields"],
            [
                "schema_version",
                "expected_response_count",
                "preregistration_commitment",
                "records",
            ],
        )
        self.assertTrue(artifact["record_count_equals_expected_response_count"])
        self.assertTrue(
            artifact[
                "response_indices_are_independently_unique_contiguous_and_zero_based"
            ]
        )
        self.assertTrue(
            artifact[
                "request_indices_are_independently_unique_contiguous_and_zero_based"
            ]
        )
        self.assertTrue(artifact["pair_uniqueness_alone_is_insufficient"])

    def test_deepseek_surface_mismatch_is_an_explicit_blocker(self) -> None:
        limitation = self.contract["mapping_binding"][
            "deepseek_mapping_v1_limitation"
        ]
        self.assertEqual(
            limitation["mapped_api_surface"],
            "openai_compatible_chat_completions",
        )
        self.assertEqual(limitation["runtime_adapter_api_surface"], "responses")
        self.assertFalse(limitation["direct_runtime_binding_allowed"])
        self.assertEqual(
            limitation["required_resolution"], "surface_keyed_mapping_successor"
        )


if __name__ == "__main__":
    unittest.main()
