from __future__ import annotations

import inspect
import unittest
from dataclasses import replace
from functools import lru_cache
from pathlib import Path

from researchops_completion_telemetry.capture import (
    CompletionCaptureError,
    CompletionTelemetryCollector,
    VerifiedCapturePlanBinding,
    verify_runtime_capture_plan,
)
from researchops_completion_telemetry.sanitization import (
    CompletionTelemetryError,
    SanitizedCompletionCapture,
    sanitize_completion_capture,
)
from researchops_completion_telemetry.surface_mapping import (
    VerifiedRuntimeCompletionBinding,
    VerifiedSurfaceSelection,
    load_and_select_surface_mapping,
)


ROOT = Path(__file__).resolve().parents[1]


@lru_cache(maxsize=1)
def _offline_selection() -> VerifiedSurfaceSelection:
    return load_and_select_surface_mapping(
        ROOT,
        "deepseek",
        "responses",
        "openai_compatible_responses",
        purpose="offline_validation",
    )


def _collector(expected: int = 1) -> CompletionTelemetryCollector:
    return CompletionTelemetryCollector.for_offline_validation(
        _offline_selection(),
        expected_response_count=expected,
        preregistration_commitment="b" * 64,
    )


def _capture(**changes: object) -> SanitizedCompletionCapture:
    raw: dict[str, object] = {
        "status": "completed",
        "incomplete_details": None,
        "usage": {
            "input_tokens": 10,
            "output_tokens": 2,
            "total_tokens": 12,
        },
        "http_status": 200,
    }
    raw.update(changes)
    return sanitize_completion_capture(
        raw,
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


class ProviderCompletionCaptureTests(unittest.TestCase):
    def test_collector_binds_exact_identity_and_positive_denominator(self) -> None:
        collector = _collector(2)
        self.assertEqual(collector.provider_id, "deepseek")
        self.assertEqual(collector.api_surface, "responses")
        self.assertEqual(collector.transport_id, "openai_compatible_responses")
        self.assertEqual(
            collector.adapter_version, "deepseek-responses-adapter/1.0"
        )
        self.assertEqual(collector.expected_response_count, 2)
        self.assertEqual(collector.observed_slot_count, 0)
        for invalid in (0, -1, True):
            with self.subTest(invalid=invalid):
                with self.assertRaises(CompletionCaptureError) as caught:
                    _collector(invalid)  # type: ignore[arg-type]
                self.assertEqual(
                    caught.exception.code,
                    "completion_capture_expected_count_invalid",
                )
        with self.assertRaises(CompletionCaptureError):
            CompletionTelemetryCollector(
                _token=object(),
                provider_id="x" * 65,
                api_surface="responses",
                transport_id="openai_responses",
                adapter_version="adapter/1.0",
                collection_purpose="offline_validation",
                offline_selection=_offline_selection(),
                runtime_plan_binding=None,
                expected_response_count=1,
                preregistration_commitment="b" * 64,
            )
        with self.assertRaises(CompletionCaptureError) as plan:
            CompletionTelemetryCollector.for_offline_validation(
                _offline_selection(),
                expected_response_count=1,
                preregistration_commitment="caller-text-not-a-commitment",
            )
        self.assertEqual(
            plan.exception.code, "completion_capture_preregistration_invalid"
        )
        forged_runtime = object.__new__(VerifiedRuntimeCompletionBinding)
        with self.assertRaises(CompletionCaptureError) as runtime:
            verify_runtime_capture_plan(
                forged_runtime, {}, preregistration_commitment="b" * 64
            )
        self.assertEqual(
            runtime.exception.code, "completion_capture_runtime_binding_required"
        )
        forged_plan = object.__new__(VerifiedCapturePlanBinding)
        with self.assertRaises(CompletionCaptureError) as plan_authority:
            CompletionTelemetryCollector.for_runtime(forged_plan)
        self.assertEqual(
            plan_authority.exception.code,
            "completion_capture_plan_authority_missing",
        )

    def test_response_index_is_internal_and_request_indices_are_contiguous(self) -> None:
        self.assertNotIn(
            "response_index", str(inspect.signature(CompletionTelemetryCollector.append))
        )
        collector = _collector(2)
        with self.assertRaises(CompletionCaptureError) as gap:
            collector.append(1, _capture())
        self.assertEqual(gap.exception.code, "completion_capture_request_index_gap")
        first = collector.append(0, _capture())
        self.assertEqual((first.request_index, first.response_index), (0, 0))
        with self.assertRaises(CompletionCaptureError) as duplicate:
            collector.append(0, _capture())
        self.assertEqual(
            duplicate.exception.code,
            "completion_capture_request_index_duplicate_or_reordered",
        )
        second = collector.append(1, _capture())
        self.assertEqual((second.request_index, second.response_index), (1, 1))

    def test_snapshot_requires_the_full_preregistered_denominator_and_seals(self) -> None:
        collector = _collector(2)
        collector.append(0, _capture())
        with self.assertRaises(CompletionCaptureError) as incomplete:
            collector.snapshot()
        self.assertEqual(
            incomplete.exception.code, "completion_capture_denominator_incomplete"
        )
        collector.append(1, _capture())
        snapshot = collector.snapshot()
        self.assertEqual(len(snapshot.slots), 2)
        self.assertEqual(snapshot.expected_response_count, 2)
        self.assertEqual(snapshot.preregistration_commitment, "b" * 64)
        self.assertEqual(snapshot.collection_purpose, "offline_validation")
        self.assertTrue(collector.sealed)
        with self.assertRaises(CompletionCaptureError) as sealed:
            collector.append_rejection(2, "completion_telemetry_sensitive_value")
        self.assertEqual(sealed.exception.code, "completion_capture_sealed")

    def test_rejection_slot_preserves_denominator_and_cannot_be_renumbered_away(self) -> None:
        collector = _collector(2)
        accepted = collector.append(0, _capture())
        rejected = collector.append_rejection(
            1, "completion_telemetry_sensitive_value"
        )
        snapshot = collector.snapshot()
        self.assertEqual(snapshot.slots, (accepted, rejected))
        self.assertEqual(rejected.capture_status, "rejected")
        self.assertIsNone(rejected.capture)
        self.assertIsNone(rejected.mapping_projection())
        self.assertEqual(
            rejected.rejection_code, "completion_telemetry_sensitive_value"
        )
        with self.assertRaises(CompletionCaptureError):
            _collector().append_rejection(0, "Unsafe Free Text")
        with self.assertRaises(CompletionCaptureError) as artifact:
            snapshot.to_record_artifact()
        self.assertEqual(
            artifact.exception.code, "completion_capture_rejected_slot_present"
        )

    def test_snapshot_builds_t2_artifact_envelope_with_frozen_denominator(self) -> None:
        collector = _collector(2)
        collector.append(0, _capture())
        collector.append(1, _capture())
        snapshot = collector.snapshot()
        artifact = snapshot.to_record_artifact()
        self.assertEqual(
            set(artifact),
            {
                "schema_version",
                "expected_response_count",
                "preregistration_commitment",
                "records",
            },
        )
        self.assertEqual(artifact["expected_response_count"], 2)
        self.assertEqual(artifact["preregistration_commitment"], "b" * 64)
        self.assertEqual(len(artifact["records"]), 2)
        self.assertEqual(
            artifact["records"][0]["native_status"]["value"], "completed"
        )
        self.assertEqual(
            artifact["records"][0]["normalized_completion_state"], "completed"
        )
        artifact["records"][0]["native_status"]["value"] = "mutated_envelope"
        rebuilt = snapshot.to_record_artifact()
        self.assertEqual(
            rebuilt["records"][0]["native_status"]["value"], "completed"
        )
        self.assertEqual(
            len(inspect.signature(snapshot.to_record_artifact).parameters), 0
        )
        with self.assertRaises(TypeError):
            snapshot.to_record_artifact(  # type: ignore[call-arg]
                [{"response_index": 0, "request_index": 0}]
            )
        for forged in (
            replace(snapshot, expected_response_count=1),
            replace(snapshot, offline_selection=None),
            replace(
                snapshot,
                slots=(
                    replace(snapshot.slots[0], provider_id="openai"),
                    snapshot.slots[1],
                ),
            ),
        ):
            with self.subTest(forged=forged):
                with self.assertRaises(CompletionCaptureError):
                    forged.to_record_artifact()

    def test_append_accepts_only_the_opaque_sanitizer_result(self) -> None:
        collector = _collector()
        with self.assertRaises(CompletionCaptureError) as caught:
            collector.append(0, {"status": "completed"})  # type: ignore[arg-type]
        self.assertEqual(
            caught.exception.code, "completion_capture_sanitized_type_required"
        )
        with self.assertRaises(CompletionTelemetryError) as construction:
            SanitizedCompletionCapture(
                _token=object(),
                mapping_projection={},
                record_components={},
                historical=False,
                canaries=(),
            )
        self.assertEqual(
            construction.exception.code,
            "completion_telemetry_capture_construction_forbidden",
        )

        class ForgedCapture(SanitizedCompletionCapture):
            pass

        forged = object.__new__(ForgedCapture)
        with self.assertRaises(CompletionCaptureError) as subclass:
            _collector().append(0, forged)
        self.assertEqual(
            subclass.exception.code, "completion_capture_sanitized_type_required"
        )

    def test_sanitizer_scans_canary_email_key_path_and_authorization_before_collector(
        self,
    ) -> None:
        cases = (
            ({"status": "CALLER-CANARY-123"}, ("CALLER-CANARY-123",)),
            ({"incomplete_details": {"reason": "person@example.test"}}, ()),
            ({"stop_sequence": "sk-test-secret-12345678"}, ()),
            ({"finish_reason": "C:\\private\\file.txt"}, ()),
            ({"status": "Authorization: Bearer secret-value"}, ()),
        )
        for changes, canaries in cases:
            raw: dict[str, object] = {
                "usage": {
                    "input_tokens": 10,
                    "output_tokens": 2,
                    "total_tokens": 12,
                }
            }
            raw.update(changes)
            with self.subTest(changes=changes):
                with self.assertRaises(CompletionTelemetryError) as caught:
                    sanitize_completion_capture(
                        raw,
                        normalized_usage={
                            "requests": 1,
                            "input_tokens": 10,
                            "output_tokens": 2,
                            "total_tokens": 12,
                            "cached_input_tokens": None,
                            "cache_write_tokens": None,
                            "reasoning_tokens": None,
                        },
                        sensitive_canaries=canaries,
                    )
                self.assertEqual(
                    caught.exception.code, "completion_telemetry_sensitive_value"
                )
                self.assertNotIn("person@example.test", str(caught.exception))
                self.assertNotIn("secret-value", str(caught.exception))

    def test_usage_domain_is_exactly_the_t2_sanitizer_domain(self) -> None:
        invalid_values = (
            [1, 2],
            {"output_tokens": True},
            {"output.tokens": 2},
            {"output_tokens": -1},
        )
        for usage in invalid_values:
            with self.subTest(usage=usage):
                with self.assertRaises(CompletionTelemetryError):
                    sanitize_completion_capture(
                        {"status": "completed", "usage": usage},
                        normalized_usage=None,
                    )

    def test_mapping_projection_excludes_usage_request_id_http_and_cap(self) -> None:
        capture = sanitize_completion_capture(
            {
                "status": "completed",
                "incomplete_details": None,
                "usage": {"input_tokens": 10, "output_tokens": 2, "total_tokens": 12},
                "provider_request_id_sha256": "a" * 64,
                "http_status": 200,
                "requested_output_token_cap": 256,
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
        self.assertEqual(
            capture.mapping_projection(),
            {"status": "completed", "incomplete_details": None},
        )
        components = capture.record_components()
        self.assertIn("usage", components)
        self.assertIn("provider_request_id_sha256", components)
        self.assertIn("http_status", components)
        self.assertIn("output_token_cap", components)

    def test_sanitized_capture_and_snapshot_are_defensively_isolated(self) -> None:
        raw = {
            "status": "completed",
            "incomplete_details": None,
            "usage": {
                "input_tokens": 10,
                "output_tokens": 2,
                "total_tokens": 12,
            },
        }
        capture = sanitize_completion_capture(
            raw,
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
        with self.assertRaises(AttributeError):
            capture._mapping_projection_bytes = b"{}"  # type: ignore[attr-defined]
        raw["status"] = "changed"
        collector = _collector()
        slot = collector.append(0, capture)
        with self.assertRaises(AttributeError):
            capture._record_components_bytes = b"{}"  # type: ignore[attr-defined]
        with self.assertRaises(AttributeError):
            capture._historical = True  # type: ignore[attr-defined]
        object.__setattr__(
            capture,
            "_mapping_projection_bytes",
            b'{"status":"tampered_after_append"}',
        )
        exposed = slot.capture
        self.assertIsNotNone(exposed)
        object.__setattr__(
            exposed,
            "_mapping_projection_bytes",
            b'{"status":"tampered_slot_copy"}',
        )
        projection = slot.mapping_projection()
        self.assertEqual(projection["status"], "completed")
        projection["status"] = "changed_snapshot"
        self.assertEqual(slot.mapping_projection()["status"], "completed")
        self.assertEqual(
            collector.snapshot().slots[0].mapping_projection()["status"],
            "completed",
        )


if __name__ == "__main__":
    unittest.main()
