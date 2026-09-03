from __future__ import annotations

import copy
import hashlib
import json
import unittest
from pathlib import Path

from researchops_completion_telemetry.sanitization import (
    CompletionTelemetryError,
    OfflineCompletionRecordBinding,
    build_offline_completion_record,
    sanitize_completion_capture,
    validate_offline_completion_record,
)
from researchops_completion_telemetry.surface_mapping import (
    load_and_select_surface_mapping,
)


ROOT = Path(__file__).resolve().parents[1]
V1_ROOT = ROOT / "evals/provider_completion_telemetry_v1"
V2_ROOT = ROOT / "evals/provider_completion_telemetry_v2"
V1_MANIFEST = V1_ROOT / "fixture_manifest.json"
V1_MAPPING = V1_ROOT / "provider_completion_mapping_v1.json"
RECORD_CONTRACT = V1_ROOT / "provider_completion_record_contract_v1.json"
RECORD_SCHEMA = V1_ROOT / "schemas/provider_completion_record_v1.schema.json"
V2_MANIFEST = V2_ROOT / "fixture_manifest_v2.json"
V2_REGISTRY = V2_ROOT / "provider_completion_surface_registry_v2.json"


EXPECTED_BASE_FIXTURES = {
    "deepseek": {
        "completed": "deepseek_completed_20260902",
        "length_capped": "deepseek_length_capped_20260902",
        "missing_fields": "deepseek_missing_fields_20260902",
        "unknown_value": "deepseek_unknown_value_20260902",
    },
    "openai": {
        "completed": "openai_completed_20260902",
        "length_capped": "openai_length_capped_20260902",
        "missing_fields": "openai_missing_fields_20260902",
        "unknown_value": "openai_unknown_value_20260902",
    },
    "anthropic": {
        "completed": "anthropic_completed_20260902",
        "length_capped": "anthropic_length_capped_20260902",
        "missing_fields": "anthropic_missing_fields_20260902",
        "unknown_value": "anthropic_unknown_value_20260902",
    },
    "moonshot_kimi": {
        "completed": "kimi_completed_20260902",
        "length_capped": "kimi_length_capped_20260902",
        "missing_fields": "kimi_missing_fields_20260902",
        "unknown_value": "kimi_unknown_value_20260902",
    },
}


def _load(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise AssertionError(f"{path} must be an object")
    return value


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _normalized_usage() -> dict[str, int | None]:
    return {
        "requests": 1,
        "input_tokens": 10,
        "output_tokens": 2,
        "total_tokens": 12,
        "cached_input_tokens": 0,
        "cache_write_tokens": None,
        "reasoning_tokens": None,
    }


def _binding() -> OfflineCompletionRecordBinding:
    return OfflineCompletionRecordBinding(
        telemetry_schema_sha256=_sha256(RECORD_SCHEMA),
        adapter_version="matrix-offline-adapter/1.0",
        mapping_schema_version="provider-completion-mapping/fixture",
        mapping_version="matrix-fixture-v1",
        mapping_sha256="a" * 64,
        provider_id="openai",
        api_surface="responses",
        transport_id="openai_responses",
    )


def _resolver(
    projection: dict[str, object] | object,
    provider_id: str,
    api_surface: str,
    transport_id: str,
) -> tuple[str, str, object, str]:
    if (provider_id, api_surface, transport_id) != (
        "openai",
        "responses",
        "openai_responses",
    ):
        raise AssertionError("offline binding mismatch")
    if not isinstance(projection, dict):
        projection = dict(projection)  # type: ignore[arg-type]
    if projection.get("status") == "completed":
        return "completed", "native_status", None, "matrix-completed-v1"
    return "unmapped", "native_status", "future", "matrix-unmapped-v1"


def _raw(**changes: object) -> dict[str, object]:
    value: dict[str, object] = {
        "status": "completed",
        "incomplete_details": None,
        "usage": {"input_tokens": 10, "output_tokens": 2, "total_tokens": 12},
        "requested_output_token_cap": 256,
    }
    value.update(changes)
    return value


def _record(**raw_changes: object) -> dict[str, object]:
    capture = sanitize_completion_capture(
        _raw(**raw_changes),
        normalized_usage=_normalized_usage(),
    )
    return build_offline_completion_record(
        capture,
        binding=_binding(),
        response_index=0,
        request_index=0,
        mapping_resolver=_resolver,
        output_counter_comparability="comparable",
        output_counter_path="output_tokens",
    )


def _record_with_canonical_size(record: dict[str, object], size: int) -> dict[str, object]:
    value = copy.deepcopy(record)
    value["matched_rule_id"] = ""
    base = len(_canonical_bytes(value))
    if size <= base:
        raise AssertionError("target record size is too small")
    value["matched_rule_id"] = "r" + "x" * (size - base - 1)
    if len(_canonical_bytes(value)) != size:
        raise AssertionError("record boundary construction failed")
    return value


class ProviderCompletionContractMatrixTests(unittest.TestCase):
    def test_each_provider_has_the_four_named_base_fixture_quadrants(self) -> None:
        manifest = _load(V1_MANIFEST)
        fixtures = manifest["fixtures"]
        self.assertIsInstance(fixtures, list)
        by_provider_scenario: dict[tuple[str, str], list[str]] = {}
        for fixture in fixtures:
            key = (fixture["provider_id"], fixture["scenario"])
            by_provider_scenario.setdefault(key, []).append(fixture["fixture_id"])

        for provider_id, expected_scenarios in EXPECTED_BASE_FIXTURES.items():
            with self.subTest(provider_id=provider_id):
                for scenario, fixture_id in expected_scenarios.items():
                    self.assertEqual(
                        by_provider_scenario.get((provider_id, scenario)),
                        [fixture_id],
                    )

    def test_raw_request_id_128_129_and_hash_only_boundaries(self) -> None:
        raw_128 = "r" * 128
        record = _record(provider_request_id=raw_128)
        self.assertEqual(
            record["provider_request_id_sha256"],
            {
                "availability": "provided",
                "value": hashlib.sha256(raw_128.encode("utf-8")).hexdigest(),
            },
        )
        self.assertNotIn(raw_128, _canonical_bytes(record).decode("utf-8"))

        with self.assertRaises(CompletionTelemetryError) as too_long:
            _record(provider_request_id="r" * 129)
        self.assertEqual(
            too_long.exception.code,
            "completion_telemetry_provider_request_id_invalid",
        )

        post_hash = _record(provider_request_id_sha256="b" * 64)
        self.assertEqual(
            post_hash["provider_request_id_sha256"],
            {"availability": "provided", "value": "b" * 64},
        )
        for invalid_hash in ("b" * 63, "B" * 64):
            with self.subTest(invalid_hash=invalid_hash):
                with self.assertRaises(CompletionTelemetryError) as invalid:
                    _record(provider_request_id_sha256=invalid_hash)
                self.assertEqual(
                    invalid.exception.code,
                    "completion_telemetry_provider_request_id_hash_invalid",
                )

    def test_http_status_99_100_599_600_boundaries(self) -> None:
        for accepted in (100, 599):
            with self.subTest(accepted=accepted):
                record = _record(http_status=accepted)
                self.assertEqual(
                    record["http_status"],
                    {"availability": "provided", "value": accepted},
                )
        for rejected in (99, 600):
            with self.subTest(rejected=rejected):
                with self.assertRaises(CompletionTelemetryError) as invalid:
                    _record(http_status=rejected)
                self.assertEqual(
                    invalid.exception.code,
                    "completion_telemetry_http_status_invalid",
                )

    def test_record_canonical_16k_boundary_and_one_byte_overflow(self) -> None:
        base = _record()
        # A schema-valid record cannot naturally approach 16 KiB because its
        # nested field caps are much tighter. Deliberately inflate rule_id so
        # this test isolates the outer byte guard: exactly 16 KiB passes that
        # guard and later fails the rule-ID cap; 16 KiB + 1 fails immediately.
        exact = _record_with_canonical_size(base, 16 * 1024)
        self.assertEqual(len(_canonical_bytes(exact)), 16 * 1024)
        with self.assertRaises(CompletionTelemetryError) as exact_result:
            validate_offline_completion_record(
                exact,
                binding=_binding(),
                mapping_resolver=_resolver,
            )
        self.assertNotEqual(
            exact_result.exception.code,
            "completion_telemetry_record_over_limit",
        )

        overflow = _record_with_canonical_size(base, 16 * 1024 + 1)
        self.assertEqual(len(_canonical_bytes(overflow)), 16 * 1024 + 1)
        with self.assertRaises(CompletionTelemetryError) as overflow_result:
            validate_offline_completion_record(
                overflow,
                binding=_binding(),
                mapping_resolver=_resolver,
            )
        self.assertEqual(
            overflow_result.exception.code,
            "completion_telemetry_record_over_limit",
        )

    def test_additional_properties_body_prompt_and_tool_arguments_are_rejected(self) -> None:
        for field in (
            "provider_response_body",
            "message_content",
            "system_prompt",
            "user_prompt",
            "tool_call_arguments",
        ):
            with self.subTest(field=field):
                raw = _raw()
                raw[field] = "must-not-survive"
                with self.assertRaises(CompletionTelemetryError) as invalid:
                    sanitize_completion_capture(
                        raw,
                        normalized_usage=_normalized_usage(),
                    )
                self.assertEqual(
                    invalid.exception.code,
                    "completion_telemetry_raw_capture_extra_field",
                )

        extra_record = _record()
        extra_record["provider_response_body"] = "must-not-survive"
        with self.assertRaises(CompletionTelemetryError) as record_error:
            validate_offline_completion_record(
                extra_record,
                binding=_binding(),
                mapping_resolver=_resolver,
            )
        self.assertEqual(
            record_error.exception.code,
            "completion_telemetry_record_shape_invalid",
        )

        nested = _record()
        nested["native_incomplete_details"]["value"] = {
            "reason": None,
            "provider_echo": "must-not-survive",
        }
        with self.assertRaises(CompletionTelemetryError) as nested_error:
            validate_offline_completion_record(
                nested,
                binding=_binding(),
                mapping_resolver=_resolver,
            )
        self.assertEqual(
            nested_error.exception.code,
            "completion_telemetry_details_capture_invalid",
        )

    def test_v1_v2_manifest_registry_and_schema_hashes_match_bytes(self) -> None:
        v1 = _load(V1_MANIFEST)
        for key in ("mapping", "rule_coverage"):
            commitment = v1[key]
            path = V1_ROOT / commitment["file"]
            self.assertEqual(path.stat().st_size, commitment["bytes"])
            self.assertEqual(_sha256(path), commitment["sha256"])
        for commitment in v1["fixtures"]:
            path = V1_ROOT / "fixtures" / commitment["file"]
            self.assertEqual(path.stat().st_size, commitment["bytes"])
            self.assertEqual(_sha256(path), commitment["sha256"])

        v2 = _load(V2_MANIFEST)
        registry_commitment = v2["registry"]
        self.assertEqual(V2_REGISTRY.stat().st_size, registry_commitment["bytes"])
        self.assertEqual(_sha256(V2_REGISTRY), registry_commitment["sha256"])
        for commitment in v2["fixtures"]:
            path = V2_ROOT / commitment["file"]
            self.assertEqual(path.stat().st_size, commitment["bytes"])
            self.assertEqual(_sha256(path), commitment["sha256"])

        registry = _load(V2_REGISTRY)
        predecessor = registry["predecessor_mapping"]
        self.assertEqual(predecessor["relative_path"], V1_MAPPING.relative_to(ROOT).as_posix())
        self.assertEqual(V1_MAPPING.stat().st_size, predecessor["bytes"])
        self.assertEqual(_sha256(V1_MAPPING), predecessor["sha256"])
        deepseek = next(
            entry for entry in registry["entries"] if entry["provider_id"] == "deepseek"
        )
        probe = ROOT / deepseek["source"]["relative_path"]
        self.assertEqual(probe.stat().st_size, deepseek["source"]["bytes"])
        self.assertEqual(_sha256(probe), deepseek["source"]["sha256"])

        contract = _load(RECORD_CONTRACT)
        schema_path = ROOT / contract["record_schema_relative_path"]
        self.assertEqual(schema_path.resolve(), RECORD_SCHEMA.resolve())
        schema_sha = _sha256(RECORD_SCHEMA)
        triples = (
            ("deepseek", "responses", "openai_compatible_responses"),
            ("openai", "responses", "openai_responses"),
            ("anthropic", "messages", "litellm_anthropic_chat_completions"),
            (
                "moonshot_kimi",
                "openai_compatible_chat_completions",
                "moonshot_direct_chat_completions_sse_v3",
            ),
        )
        for provider_id, api_surface, transport_id in triples:
            selection = load_and_select_surface_mapping(
                ROOT,
                provider_id,
                api_surface,
                transport_id,
                purpose="offline_validation",
            )
            self.assertEqual(selection.telemetry_schema_sha256, schema_sha)

if __name__ == "__main__":
    unittest.main()
