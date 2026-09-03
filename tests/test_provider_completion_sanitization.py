from __future__ import annotations

import ast
import copy
import hashlib
import inspect
import json
import unittest
from pathlib import Path
from unittest.mock import patch

from jsonschema import Draft202012Validator

from researchops_completion_telemetry.sanitization import (
    ARTIFACT_SCHEMA_VERSION,
    CompletionTelemetryError,
    MISSING,
    OfflineCompletionRecordBinding,
    SanitizedCompletionCapture,
    build_completion_record as build_live_completion_record,
    build_offline_completion_record,
    hash_provider_request_id,
    sanitize_completion_capture,
    validate_completion_artifact as validate_live_completion_artifact,
    validate_completion_record as validate_live_completion_record,
    validate_offline_completion_artifact,
    validate_offline_completion_record,
)
from researchops_completion_telemetry.surface_mapping import (
    VerifiedRuntimeCompletionBinding,
)
from researchops_completion_telemetry.capture import VerifiedCapturePlanBinding


# The vectors in this file are schema/fixture tests and intentionally use the
# non-closure-eligible API. Live entrypoints are referenced explicitly only by
# the runtime-authority red-team tests.
build_completion_record = build_offline_completion_record
validate_completion_record = validate_offline_completion_record
validate_completion_artifact = validate_offline_completion_artifact


ROOT = Path(__file__).resolve().parents[1]
SCHEMA_PATH = (
    ROOT
    / "evals"
    / "provider_completion_telemetry_v1"
    / "schemas"
    / "provider_completion_record_v1.schema.json"
)
MODULE_PATH = ROOT / "src" / "researchops_completion_telemetry" / "sanitization.py"
SCHEMA_SHA256 = hashlib.sha256(SCHEMA_PATH.read_bytes()).hexdigest()


def _binding(**changes: str) -> OfflineCompletionRecordBinding:
    values = {
        "telemetry_schema_sha256": SCHEMA_SHA256,
        "adapter_version": "openai-responses-adapter/1.0",
        "mapping_schema_version": "provider-completion-mapping/2.0",
        "mapping_version": "openai-responses-v1",
        "mapping_sha256": "a" * 64,
        "provider_id": "openai",
        "api_surface": "responses",
        "transport_id": "openai_responses",
    }
    values.update(changes)
    return OfflineCompletionRecordBinding(**values)


def _normalized_usage(**changes: int | None) -> dict[str, int | None]:
    values: dict[str, int | None] = {
        "requests": 1,
        "input_tokens": 10,
        "output_tokens": 5,
        "total_tokens": 15,
        "cached_input_tokens": 0,
        "cache_write_tokens": None,
        "reasoning_tokens": None,
    }
    values.update(changes)
    return values


def _resolver(
    projection: dict[str, object] | object,
    provider_id: str,
    api_surface: str,
    transport_id: str,
) -> tuple[str, str, object, str]:
    if not isinstance(projection, dict):
        projection = dict(projection)  # type: ignore[arg-type]
    if not provider_id or not api_surface or not transport_id:
        raise AssertionError("binding key missing")
    status = projection.get("status")
    details = projection.get("incomplete_details")
    reason = details.get("reason") if isinstance(details, dict) else None
    if status == "completed":
        return "completed", "native_status", None, "openai-resp-recognized-001"
    if status == "incomplete" and reason == "max_output_tokens":
        return (
            "incomplete_length",
            "native_status",
            None,
            "openai-resp-recognized-004",
        )
    non_null = [
        projection.get(field)
        for field in ("status", "finish_reason", "stop_reason", "stop_sequence")
        if projection.get(field) is not None
    ]
    if non_null or (isinstance(details, dict) and details):
        preserved = non_null[0] if non_null and isinstance(non_null[0], str) else None
        return "unmapped", "native_status", preserved, "test-unmapped-v1"
    return "not_provided", "none", None, "openai-resp-absent-001"


def _raw_completed() -> dict[str, object]:
    return {
        "status": "completed",
        "incomplete_details": None,
        "usage": {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
        "provider_request_id": "req_safe_123",
        "http_status": 200,
        "requested_output_token_cap": 256,
    }


def _build(
    raw: dict[str, object] | None = None,
    *,
    binding: OfflineCompletionRecordBinding | None = None,
    normalized_usage: dict[str, int | None] | None | object = MISSING,
    historical: bool = False,
    canaries: tuple[str, ...] = (),
    comparability: str = "comparable",
    counter_path: str | None = "output_tokens",
) -> dict[str, object]:
    if historical:
        raw_value: dict[str, object] = {}
        normalized = None
        resolver = None
        comparability = "not_persisted"
        counter_path = None
    else:
        raw_value = copy.deepcopy(raw if raw is not None else _raw_completed())
        normalized = (
            _normalized_usage() if normalized_usage is MISSING else normalized_usage
        )
        resolver = _resolver
    capture = sanitize_completion_capture(
        raw_value,
        normalized_usage=normalized,  # type: ignore[arg-type]
        historical=historical,
        sensitive_canaries=canaries,
    )
    return build_offline_completion_record(
        capture,
        binding=binding or _binding(),
        response_index=0,
        request_index=0,
        mapping_resolver=resolver,
        output_counter_comparability=comparability,
        output_counter_path=counter_path,
    )


def _raw_details_with_exact_size(size: int) -> dict[str, object]:
    value: dict[str, object] = {"padding": "", "reason": "max_output_tokens"}
    base = len(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    )
    if size < base:
        raise AssertionError("target size too small")
    value["padding"] = "x" * (size - base)
    actual = len(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    )
    if actual != size:
        raise AssertionError((size, actual))
    return value


class ProviderCompletionSanitizationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
        Draft202012Validator.check_schema(cls.schema)
        cls.schema_validator = Draft202012Validator(cls.schema)

    def assertSchemaValid(self, record: dict[str, object]) -> None:
        errors = sorted(
            self.schema_validator.iter_errors(record), key=lambda item: list(item.path)
        )
        self.assertEqual([item.message for item in errors], [])

    def test_builds_exact_schema_record_and_never_persists_raw_request_id(self) -> None:
        record = _build()
        self.assertSchemaValid(record)
        validate_completion_record(
            record,
            binding=_binding(),
            mapping_resolver=_resolver,
        )
        self.assertEqual(record["record_provenance"], "offline_validation")
        self.assertEqual(record["telemetry_schema_sha256"], SCHEMA_SHA256)
        request_hash = record["provider_request_id_sha256"]
        self.assertEqual(request_hash["availability"], "provided")
        self.assertEqual(
            request_hash["value"], hashlib.sha256(b"req_safe_123").hexdigest()
        )
        serialized = json.dumps(record, sort_keys=True)
        self.assertNotIn("req_safe_123", serialized)
        self.assertNotIn("provider_request_id\"", serialized)
        self.assertEqual(record["output_token_cap"]["value"], 256)
        self.assertEqual(
            hash_provider_request_id("req_safe_123"), request_hash["value"]
        )

        post_hash = _raw_completed()
        post_hash.pop("provider_request_id")
        post_hash["provider_request_id_sha256"] = "d" * 64
        collected_record = _build(post_hash)
        self.assertEqual(
            collected_record["provider_request_id_sha256"],
            {"availability": "provided", "value": "d" * 64},
        )
        ambiguous = _raw_completed()
        ambiguous["provider_request_id_sha256"] = "d" * 64
        with self.assertRaises(CompletionTelemetryError) as caught:
            _build(ambiguous)
        self.assertEqual(
            caught.exception.code,
            "completion_telemetry_provider_request_id_ambiguous",
        )

    def test_missing_explicit_null_and_historical_not_persisted_are_distinct(self) -> None:
        missing_raw = {
            "usage": None,
            "provider_request_id": None,
            "http_status": None,
            "requested_output_token_cap": None,
        }
        missing = _build(
            missing_raw,
            normalized_usage=None,
            comparability="not_provided",
            counter_path=None,
        )
        explicit_null = _build(
            {**missing_raw, "status": None, "incomplete_details": None},
            normalized_usage=None,
            comparability="not_provided",
            counter_path=None,
        )
        historical = _build(historical=True)
        self.assertEqual(missing["native_status"]["availability"], "not_provided")
        self.assertEqual(explicit_null["native_status"]["availability"], "provided")
        self.assertIsNone(explicit_null["native_status"]["value"])
        self.assertEqual(
            historical["native_status"]["availability"], "not_persisted"
        )
        self.assertEqual(historical["record_provenance"], "historical_projection")
        self.assertEqual(historical["normalized_completion_state"], "not_persisted")
        self.assertSchemaValid(missing)
        self.assertSchemaValid(explicit_null)
        self.assertSchemaValid(historical)

        live_spoof = copy.deepcopy(historical)
        live_spoof["record_provenance"] = "live_adapter_write"
        with self.assertRaises(CompletionTelemetryError) as caught:
            validate_completion_record(
                live_spoof,
                binding=_binding(),
                mapping_resolver=_resolver,
            )
        self.assertEqual(
            caught.exception.code,
            "completion_telemetry_record_provenance_invalid",
        )

    def test_sensitive_values_are_rejected_before_unknown_children_are_omitted(self) -> None:
        cases = (
            ("api_key", "sk-testsecret123456"),
            ("authorization", "Authorization: Bearer fake-secret-token"),
            ("cookie", "Cookie: session=fake-secret"),
            ("windows_path", r"C:\\Users\\analyst\\private.csv"),
            ("unc_path", r"\\\\server\\share\\private.csv"),
            ("posix_path", "/home/analyst/private.csv"),
            ("traceback", 'Traceback (most recent call last): File "x.py", line 7'),
            ("email", "person@example.test"),
            ("caller_canary", "CALLER-SUPPLIED-CANARY-987"),
        )
        for label, value in cases:
            with self.subTest(label=label):
                raw = _raw_completed()
                raw["incomplete_details"] = {
                    "reason": "max_output_tokens",
                    "provider_note": value,
                }
                canaries = (value,) if label == "caller_canary" else ()
                with self.assertRaises(CompletionTelemetryError) as caught:
                    _build(raw, canaries=canaries)
                self.assertIn(
                    caught.exception.code,
                    {
                        "completion_telemetry_sensitive_value",
                        "completion_telemetry_sensitive_field",
                    },
                )
                self.assertNotIn(value, str(caught.exception))

    def test_mapping_critical_scalars_use_utf8_byte_limits_without_mutation(self) -> None:
        accepted = _raw_completed()
        accepted["status"] = "测" * 21  # 63 UTF-8 bytes.
        record = _build(accepted)
        self.assertEqual(record["native_status"]["value"], "测" * 21)
        self.assertFalse(record["native_status"]["truncated"])
        self.assertEqual(record["normalized_completion_state"], "unmapped")

        rejected = _raw_completed()
        rejected["status"] = "测" * 22  # 66 UTF-8 bytes.
        with self.assertRaises(CompletionTelemetryError) as caught:
            _build(rejected)
        self.assertEqual(
            caught.exception.code,
            "completion_telemetry_mapping_critical_value_over_limit",
        )

    def test_stop_sequence_truncates_on_utf8_boundary_with_bidirectional_marker(self) -> None:
        ordinary = _raw_completed()
        ordinary.pop("status")
        ordinary["stop_sequence"] = "\n\nHuman:"
        ordinary_record = _build(ordinary)
        self.assertEqual(
            ordinary_record["native_stop_sequence"]["value"], "\n\nHuman:"
        )

        raw = _raw_completed()
        raw.pop("status")
        raw["stop_sequence"] = "测" * 30
        record = _build(raw)
        capture = record["native_stop_sequence"]
        self.assertTrue(capture["truncated"])
        self.assertTrue(capture["value"].endswith("[TRUNCATED]"))
        self.assertLessEqual(len(capture["value"].encode("utf-8")), 64)

        marker_spoof = copy.deepcopy(record)
        marker_spoof["native_stop_sequence"]["truncated"] = False
        with self.assertRaises(CompletionTelemetryError) as caught:
            validate_completion_record(
                marker_spoof,
                binding=_binding(),
                mapping_resolver=_resolver,
            )
        self.assertEqual(
            caught.exception.code, "completion_telemetry_reserved_marker_spoofed"
        )

        raw_spoof = _raw_completed()
        raw_spoof.pop("status")
        raw_spoof["stop_sequence"] = "prefix[TRUNCATED]"
        with self.assertRaises(CompletionTelemetryError) as raw_caught:
            _build(raw_spoof)
        self.assertEqual(
            raw_caught.exception.code,
            "completion_telemetry_reserved_marker_spoofed",
        )

    def test_incomplete_details_raw_canonical_511_512_513_boundaries(self) -> None:
        results: dict[int, dict[str, object]] = {}
        for size in (511, 512, 513):
            raw = _raw_completed()
            raw["status"] = "incomplete"
            raw["incomplete_details"] = _raw_details_with_exact_size(size)
            results[size] = _build(raw)
        for size in (511, 512):
            details = results[size]["native_incomplete_details"]
            self.assertFalse(details["truncated"])
            self.assertNotIn("_telemetry_truncated", details["value"])
            self.assertEqual(details["omitted_child_field_count"], 1)
        details_513 = results[513]["native_incomplete_details"]
        self.assertTrue(details_513["truncated"])
        self.assertIs(details_513["value"]["_telemetry_truncated"], True)
        self.assertEqual(details_513["value"]["reason"], "max_output_tokens")

        multibyte_raw = _raw_completed()
        multibyte_raw["status"] = "incomplete"
        multibyte_details = _raw_details_with_exact_size(512)
        padding = multibyte_details["padding"]
        self.assertIsInstance(padding, str)
        # Replace three ASCII bytes with one three-byte code point: byte size
        # stays exactly 512 while the character count changes.
        multibyte_details["padding"] = "测" + padding[3:]
        self.assertEqual(
            len(
                json.dumps(
                    multibyte_details,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ),
            512,
        )
        multibyte_raw["incomplete_details"] = multibyte_details
        multibyte_record = _build(multibyte_raw)
        self.assertFalse(multibyte_record["native_incomplete_details"]["truncated"])

    def test_incomplete_reason_has_independent_multibyte_64_byte_limit(self) -> None:
        raw = _raw_completed()
        raw.pop("status")
        raw["incomplete_details"] = {"reason": "测" * 21}
        record = _build(raw)
        self.assertEqual(
            record["native_incomplete_details"]["value"]["reason"], "测" * 21
        )
        raw["incomplete_details"] = {"reason": "测" * 22}
        with self.assertRaises(CompletionTelemetryError) as caught:
            _build(raw)
        self.assertEqual(
            caught.exception.code,
            "completion_telemetry_mapping_critical_value_over_limit",
        )

    def test_usage_keeps_numeric_and_null_leaves_and_marks_64_leaf_overflow(self) -> None:
        sixty_four = {f"counter_{index:03d}": index for index in range(64)}
        raw = _raw_completed()
        raw["usage"] = sixty_four
        record = _build(
            raw,
            normalized_usage=_normalized_usage(),
            comparability="not_provided",
            counter_path=None,
        )
        usage = record["usage"]
        self.assertEqual(len(usage["native_numeric_counters"]), 64)
        self.assertFalse(usage["truncated"])

        raw["usage"] = {**sixty_four, "counter_999": None}
        overflow = _build(
            raw,
            normalized_usage=_normalized_usage(),
            comparability="not_provided",
            counter_path=None,
        )
        self.assertEqual(len(overflow["usage"]["native_numeric_counters"]), 64)
        self.assertTrue(overflow["usage"]["truncated"])
        self.assertFalse(overflow["usage"]["complete"])

    def test_usage_missing_null_and_empty_object_remain_distinct(self) -> None:
        missing_raw = _raw_completed()
        missing_raw.pop("usage")
        missing = _build(
            missing_raw,
            normalized_usage=None,
            comparability="not_provided",
            counter_path=None,
        )
        null_raw = _raw_completed()
        null_raw["usage"] = None
        explicit_null = _build(
            null_raw,
            normalized_usage=None,
            comparability="not_provided",
            counter_path=None,
        )
        empty_raw = _raw_completed()
        empty_raw["usage"] = {}
        empty_object = _build(
            empty_raw,
            normalized_usage=_normalized_usage(
                requests=None,
                input_tokens=None,
                output_tokens=None,
                total_tokens=None,
                cached_input_tokens=None,
            ),
            comparability="not_provided",
            counter_path=None,
        )
        self.assertEqual(missing["usage"]["native_value_state"], "not_provided")
        self.assertEqual(explicit_null["usage"]["native_value_state"], "null")
        self.assertEqual(empty_object["usage"]["native_value_state"], "object")
        self.assertSchemaValid(missing)
        self.assertSchemaValid(explicit_null)
        self.assertSchemaValid(empty_object)

    def test_usage_depth_and_4k_limits_are_explicit_not_silent(self) -> None:
        raw = _raw_completed()
        raw["usage"] = {"a": {"b": {"c": {"d": {"e": 1}}}}}
        depth = _build(
            raw,
            normalized_usage=_normalized_usage(),
            comparability="not_provided",
            counter_path=None,
        )
        self.assertTrue(depth["usage"]["truncated"])
        self.assertEqual(depth["usage"]["native_numeric_counters"], [])

        long_usage = {
            f"counter_{index:03d}_{'x' * 44}": index for index in range(64)
        }
        raw["usage"] = long_usage
        bounded = _build(
            raw,
            normalized_usage=_normalized_usage(),
            comparability="not_provided",
            counter_path=None,
        )
        encoded = json.dumps(
            bounded["usage"],
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        self.assertLessEqual(len(encoded), 4096)
        self.assertTrue(bounded["usage"]["truncated"])
        self.assertFalse(bounded["usage"]["complete"])

    def test_usage_bool_float_negative_nonfinite_and_sensitive_omitted_text_fail(self) -> None:
        cases = (
            (True, "completion_telemetry_usage_boolean_invalid"),
            (-1, "completion_telemetry_usage_counter_invalid"),
            (1.5, "completion_telemetry_usage_counter_invalid"),
            (float("nan"), "completion_telemetry_nonfinite_number"),
            (float("inf"), "completion_telemetry_nonfinite_number"),
            (
                "Authorization: Bearer usage-secret-value",
                "completion_telemetry_sensitive_value",
            ),
        )
        for value, code in cases:
            with self.subTest(code=code):
                raw = _raw_completed()
                raw["usage"] = {"output_tokens": value}
                with self.assertRaises(CompletionTelemetryError) as caught:
                    _build(raw, normalized_usage=_normalized_usage())
                self.assertEqual(caught.exception.code, code)

    def test_safe_non_numeric_usage_is_omitted_and_makes_usage_incomplete(self) -> None:
        raw = _raw_completed()
        raw["usage"] = {
            "input_tokens": 10,
            "output_tokens": 5,
            "total_tokens": 15,
            "service_tier": "standard",
        }
        record = _build(raw)
        self.assertEqual(record["usage"]["omitted_non_numeric_field_count"], 1)
        self.assertFalse(record["usage"]["complete"])
        self.assertNotIn("standard", json.dumps(record, sort_keys=True))

    def test_extra_raw_record_and_nested_fields_fail_closed(self) -> None:
        raw = _raw_completed()
        raw["message_content"] = "must not enter telemetry"
        with self.assertRaises(CompletionTelemetryError) as caught:
            _build(raw)
        self.assertEqual(
            caught.exception.code, "completion_telemetry_raw_capture_extra_field"
        )

        record = _build()
        record["native_status"]["raw_value"] = "completed"
        with self.assertRaises(CompletionTelemetryError) as nested:
            validate_completion_record(
                record,
                binding=_binding(),
                mapping_resolver=_resolver,
            )
        self.assertEqual(
            nested.exception.code, "completion_telemetry_scalar_capture_invalid"
        )

    def test_offline_validator_replays_provider_surface_transport_mapping(self) -> None:
        calls: list[tuple[str, str, str]] = []

        def resolver(projection, provider_id, api_surface, transport_id):
            self.assertTrue(
                set(projection)
                <= {
                    "status",
                    "finish_reason",
                    "stop_reason",
                    "stop_sequence",
                    "incomplete_details",
                }
            )
            calls.append((provider_id, api_surface, transport_id))
            return _resolver(projection, provider_id, api_surface, transport_id)

        capture = sanitize_completion_capture(
            _raw_completed(),
            normalized_usage=_normalized_usage(),
        )
        record = build_completion_record(
            capture,
            binding=_binding(),
            response_index=0,
            request_index=0,
            mapping_resolver=resolver,
            output_counter_comparability="comparable",
            output_counter_path="output_tokens",
        )
        self.assertEqual(
            calls,
            [
                ("openai", "responses", "openai_responses"),
                ("openai", "responses", "openai_responses"),
            ],
        )
        tampered = copy.deepcopy(record)
        tampered["normalized_completion_state"] = "incomplete_other"
        with self.assertRaises(CompletionTelemetryError) as caught:
            validate_completion_record(
                tampered,
                binding=_binding(),
                mapping_resolver=resolver,
            )
        self.assertEqual(
            caught.exception.code, "completion_telemetry_mapping_result_mismatch"
        )

    def test_only_factory_capture_is_accepted_and_copies_are_defensive(self) -> None:
        raw = _raw_completed()
        capture = sanitize_completion_capture(
            raw,
            normalized_usage=_normalized_usage(),
        )
        raw["status"] = "tampered_after_sanitize"
        projection = capture.mapping_projection()
        self.assertEqual(set(projection), {"status", "incomplete_details"})
        self.assertTrue(
            {"usage", "provider_request_id_sha256", "http_status"}.isdisjoint(
                projection
            )
        )
        projection["status"] = "tampered_copy"
        components = capture.record_components()
        components["native_status"]["value"] = "tampered_copy"
        self.assertEqual(capture.mapping_projection()["status"], "completed")
        self.assertEqual(
            capture.record_components()["native_status"]["value"], "completed"
        )
        with self.assertRaises(AttributeError):
            capture._historical = True  # type: ignore[misc]
        with self.assertRaises(AttributeError):
            capture._mapping_projection_bytes = b"{}"  # type: ignore[misc]
        self.assertIsInstance(capture._mapping_projection_bytes, bytes)
        self.assertIsInstance(capture._record_components_bytes, bytes)

        with self.assertRaises(CompletionTelemetryError) as constructor:
            SanitizedCompletionCapture(
                _token=object(),
                mapping_projection={},
                record_components={},
                historical=False,
                canaries=(),
            )
        self.assertEqual(
            constructor.exception.code,
            "completion_telemetry_capture_construction_forbidden",
        )
        with self.assertRaises(CompletionTelemetryError) as ordinary_mapping:
            build_completion_record(
                _raw_completed(),  # type: ignore[arg-type]
                binding=_binding(),
                response_index=0,
                request_index=0,
                mapping_resolver=_resolver,
            )
        self.assertEqual(
            ordinary_mapping.exception.code,
            "completion_telemetry_sanitized_capture_required",
        )

        class ForgedCapture(SanitizedCompletionCapture):
            pass

        forged = object.__new__(ForgedCapture)
        with self.assertRaises(CompletionTelemetryError) as subclass:
            build_completion_record(
                forged,
                binding=_binding(),
                response_index=0,
                request_index=0,
                mapping_resolver=_resolver,
            )
        self.assertEqual(
            subclass.exception.code,
            "completion_telemetry_sanitized_capture_required",
        )

    def test_provider_mapping_cannot_emit_token_cap_fallback(self) -> None:
        capture = sanitize_completion_capture(
            _raw_completed(), normalized_usage=_normalized_usage()
        )

        def invalid_resolver(*_args):
            return (
                "incomplete_length",
                "token_cap_fallback",
                None,
                "provider-forged-fallback-v1",
            )

        with self.assertRaises(CompletionTelemetryError) as caught:
            build_completion_record(
                capture,
                binding=_binding(),
                response_index=0,
                request_index=0,
                mapping_resolver=invalid_resolver,
                output_counter_comparability="comparable",
                output_counter_path="output_tokens",
            )
        self.assertEqual(
            caught.exception.code,
            "completion_telemetry_provider_mapping_fallback_forbidden",
        )

    def test_live_entrypoints_require_unforgeable_runtime_binding(self) -> None:
        capture = sanitize_completion_capture(
            _raw_completed(), normalized_usage=_normalized_usage()
        )
        with self.assertRaises(CompletionTelemetryError) as offline_binding:
            build_live_completion_record(
                capture,
                binding=_binding(),  # type: ignore[arg-type]
                response_index=0,
                request_index=0,
            )
        self.assertEqual(
            offline_binding.exception.code,
            "completion_telemetry_runtime_binding_required",
        )

        forged = object.__new__(VerifiedRuntimeCompletionBinding)
        with self.assertRaises(CompletionTelemetryError) as forged_binding:
            build_live_completion_record(
                capture,
                binding=forged,
                response_index=0,
                request_index=0,
            )
        self.assertEqual(
            forged_binding.exception.code,
            "completion_telemetry_runtime_binding_required",
        )

        offline_record = _build()
        offline_artifact = {
            "schema_version": ARTIFACT_SCHEMA_VERSION,
            "expected_response_count": 1,
            "preregistration_commitment": "e" * 64,
            "records": [offline_record],
        }
        with self.assertRaises(CompletionTelemetryError) as artifact_binding:
            validate_live_completion_artifact(
                offline_artifact,
                plan_binding=object.__new__(VerifiedCapturePlanBinding),
            )
        self.assertEqual(
            artifact_binding.exception.code,
            "completion_telemetry_capture_plan_binding_required",
        )

        live_parameters = inspect.signature(build_live_completion_record).parameters
        self.assertNotIn("mapping_resolver", live_parameters)
        self.assertNotIn("output_counter_comparability", live_parameters)
        self.assertNotIn("output_counter_path", live_parameters)
        validate_parameters = inspect.signature(
            validate_live_completion_record
        ).parameters
        self.assertNotIn("mapping_resolver", validate_parameters)
        artifact_parameters = inspect.signature(
            validate_live_completion_artifact
        ).parameters
        self.assertNotIn("mapping_resolver", artifact_parameters)
        self.assertIn("plan_binding", artifact_parameters)

    def test_caller_chosen_input_counter_can_only_create_offline_provenance(self) -> None:
        raw = {
            "usage": {"input_tokens": 16, "output_tokens": 2, "total_tokens": 18},
            "requested_output_token_cap": 16,
        }
        capture = sanitize_completion_capture(
            raw,
            normalized_usage=_normalized_usage(
                input_tokens=16,
                output_tokens=2,
                total_tokens=18,
            ),
        )

        def absent_resolver(*_args):
            return "not_provided", "none", None, "openai-resp-absent-001"

        offline = build_offline_completion_record(
            capture,
            binding=_binding(),
            response_index=0,
            request_index=0,
            mapping_resolver=absent_resolver,
            output_counter_comparability="comparable",
            output_counter_path="input_tokens",
        )
        self.assertEqual(offline["record_provenance"], "offline_validation")
        self.assertEqual(offline["truncation_signal_source"], "token_cap_fallback")
        with self.assertRaises(CompletionTelemetryError) as live_validation:
            validate_live_completion_record(
                offline,
                binding=_binding(),  # type: ignore[arg-type]
            )
        self.assertEqual(
            live_validation.exception.code,
            "completion_telemetry_runtime_binding_required",
        )

    def test_token_cap_fallback_requires_exact_persisted_counter_and_cap(self) -> None:
        raw = {
            "usage": {"input_tokens": 10, "output_tokens": 16, "total_tokens": 26},
            "provider_request_id": "req_fallback_1",
            "http_status": 200,
            "requested_output_token_cap": 16,
        }

        def fallback_resolver(projection, provider_id, api_surface, transport_id):
            del provider_id, api_surface, transport_id
            self.assertNotIn("status", projection)
            self.assertNotIn("usage", projection)
            self.assertNotIn("provider_request_id_sha256", projection)
            self.assertNotIn("http_status", projection)
            return "not_provided", "none", None, "openai-resp-absent-001"

        capture = sanitize_completion_capture(
            raw,
            normalized_usage=_normalized_usage(output_tokens=16, total_tokens=26),
        )
        record = build_completion_record(
            capture,
            binding=_binding(),
            response_index=0,
            request_index=0,
            mapping_resolver=fallback_resolver,
            output_counter_comparability="comparable",
            output_counter_path="output_tokens",
        )
        self.assertEqual(record["output_token_cap"]["value"], 16)
        self.assertEqual(record["truncation_signal_source"], "token_cap_fallback")

        mismatch = copy.deepcopy(record)
        mismatch["output_token_cap"]["value"] = 17
        with self.assertRaises(CompletionTelemetryError) as caught:
            validate_completion_record(
                mismatch,
                binding=_binding(),
                mapping_resolver=fallback_resolver,
            )
        self.assertEqual(
            caught.exception.code, "completion_telemetry_token_cap_fallback_invalid"
        )

    def test_mapping_resolver_error_is_replaced_by_stable_non_sensitive_code(self) -> None:
        secret = "sk-resolver-secret-123456"

        def failing_resolver(*_args):
            raise RuntimeError(secret)

        with self.assertRaises(CompletionTelemetryError) as caught:
            capture = sanitize_completion_capture(
                _raw_completed(),
                normalized_usage=_normalized_usage(),
            )
            build_completion_record(
                capture,
                binding=_binding(),
                response_index=0,
                request_index=0,
                mapping_resolver=failing_resolver,
                output_counter_comparability="comparable",
                output_counter_path="output_tokens",
            )
        self.assertEqual(
            caught.exception.code, "completion_telemetry_mapping_resolver_failed"
        )
        self.assertNotIn(secret, str(caught.exception))

    def test_unmapped_requires_nonempty_native_evidence(self) -> None:
        record = _build()
        empty = copy.deepcopy(record)
        empty.update(
            {
                "native_status": {
                    "availability": "provided",
                    "value": None,
                    "redaction_applied": False,
                    "truncated": False,
                },
                "native_incomplete_details": {
                    "availability": "provided",
                    "value": {},
                    "redaction_applied": False,
                    "truncated": False,
                    "omitted_child_field_count": 0,
                },
                "normalized_completion_state": "unmapped",
                "truncation_signal_source": "native_status",
                "matched_rule_id": "test-unmapped-v1",
            }
        )

        def vacuous_resolver(*_args):
            return "unmapped", "native_status", None, "test-unmapped-v1"

        with self.assertRaises(CompletionTelemetryError) as caught:
            validate_completion_record(
                empty,
                binding=_binding(),
                mapping_resolver=vacuous_resolver,
            )
        self.assertEqual(
            caught.exception.code,
            "completion_telemetry_unmapped_without_native_evidence",
        )

        marker_only = copy.deepcopy(empty)
        marker_only["native_incomplete_details"].update(
            {
                "value": {"_telemetry_truncated": True},
                "truncated": True,
            }
        )
        with self.assertRaises(CompletionTelemetryError) as marker_caught:
            validate_completion_record(
                marker_only,
                binding=_binding(),
                mapping_resolver=vacuous_resolver,
            )
        self.assertEqual(
            marker_caught.exception.code,
            "completion_telemetry_unmapped_without_native_evidence",
        )

    def test_artifact_validator_revalidates_every_record_and_scans_canaries(self) -> None:
        first = _build()
        second = _build()
        second["response_index"] = 1
        second["request_index"] = 1
        artifact = {
            "schema_version": ARTIFACT_SCHEMA_VERSION,
            "expected_response_count": 2,
            "preregistration_commitment": "e" * 64,
            "records": [first, second],
        }
        validate_completion_artifact(
            artifact,
            binding=_binding(),
            mapping_resolver=_resolver,
            sensitive_canaries=("ARTIFACT-CANARY-123",),
        )

        duplicate = copy.deepcopy(artifact)
        duplicate["records"][1]["response_index"] = 0
        with self.assertRaises(CompletionTelemetryError) as duplicate_error:
            validate_completion_artifact(
                duplicate,
                binding=_binding(),
                mapping_resolver=_resolver,
            )
        self.assertEqual(
            duplicate_error.exception.code,
            "completion_telemetry_artifact_duplicate_response_index",
        )

        duplicate_request = copy.deepcopy(artifact)
        duplicate_request["records"][1]["request_index"] = 0
        with self.assertRaises(CompletionTelemetryError) as request_error:
            validate_completion_artifact(
                duplicate_request,
                binding=_binding(),
                mapping_resolver=_resolver,
            )
        self.assertEqual(
            request_error.exception.code,
            "completion_telemetry_artifact_duplicate_request_index",
        )

        response_gap = copy.deepcopy(artifact)
        response_gap["records"][1]["response_index"] = 2
        with self.assertRaises(CompletionTelemetryError) as response_gap_error:
            validate_completion_artifact(
                response_gap,
                binding=_binding(),
                mapping_resolver=_resolver,
            )
        self.assertEqual(
            response_gap_error.exception.code,
            "completion_telemetry_artifact_response_index_gap",
        )

        request_gap = copy.deepcopy(artifact)
        request_gap["records"][1]["request_index"] = 2
        with self.assertRaises(CompletionTelemetryError) as request_gap_error:
            validate_completion_artifact(
                request_gap,
                binding=_binding(),
                mapping_resolver=_resolver,
            )
        self.assertEqual(
            request_gap_error.exception.code,
            "completion_telemetry_artifact_request_index_gap",
        )

        denominator = copy.deepcopy(artifact)
        denominator["expected_response_count"] = 3
        with self.assertRaises(CompletionTelemetryError) as denominator_error:
            validate_completion_artifact(
                denominator,
                binding=_binding(),
                mapping_resolver=_resolver,
            )
        self.assertEqual(
            denominator_error.exception.code,
            "completion_telemetry_artifact_denominator_mismatch",
        )

        canary = copy.deepcopy(artifact)
        canary["records"][1]["native_stop_sequence"] = {
            "availability": "provided",
            "value": "ARTIFACT-CANARY-123",
            "redaction_applied": False,
            "truncated": False,
        }
        with self.assertRaises(CompletionTelemetryError) as canary_error:
            validate_completion_artifact(
                canary,
                binding=_binding(),
                mapping_resolver=_resolver,
                sensitive_canaries=("ARTIFACT-CANARY-123",),
            )
        self.assertEqual(
            canary_error.exception.code, "completion_telemetry_sensitive_value"
        )

        extra = copy.deepcopy(artifact)
        extra["summary"] = {}
        with self.assertRaises(CompletionTelemetryError) as shape_error:
            validate_completion_artifact(
                extra,
                binding=_binding(),
                mapping_resolver=_resolver,
            )
        self.assertEqual(
            shape_error.exception.code, "completion_telemetry_artifact_shape_invalid"
        )

    def test_pure_module_performs_no_io_and_imports_no_researchops(self) -> None:
        source = MODULE_PATH.read_text(encoding="utf-8")
        tree = ast.parse(source)
        imported = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.append(node.module)
        self.assertFalse(
            any(
                name == "researchops" or name.startswith("researchops.")
                for name in imported
            )
        )
        forbidden_calls = {"open", "Path", "urlopen", "request", "connect"}
        call_names = {
            node.func.id
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        }
        self.assertTrue(forbidden_calls.isdisjoint(call_names))

        with patch("builtins.open", side_effect=AssertionError("I/O forbidden")):
            _build()


if __name__ == "__main__":
    unittest.main()
