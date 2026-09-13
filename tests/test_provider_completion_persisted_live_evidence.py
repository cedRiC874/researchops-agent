from __future__ import annotations

import ast
import copy
import hashlib
import inspect
import json
import os
import subprocess
import sys
import unittest
from dataclasses import FrozenInstanceError, asdict
from pathlib import Path
from unittest.mock import patch

import researchops_completion_telemetry.capture as capture_module
import researchops_completion_telemetry.sanitization as sanitization_module
import researchops_completion_telemetry.surface_mapping as surface_mapping_module
from researchops_completion_telemetry.persisted_evidence import (
    PersistedLiveEvidenceValidationContext,
    PersistedRuntimeAttemptSummary,
    PersistedRuntimeCaseSummary,
    PersistedRuntimeDenominatorSummary,
    PersistedRuntimeResponseSummary,
    PersistedRuntimeUsageIndexSummary,
    create_persisted_live_evidence_validation_context,
    summarize_persisted_live_runtime_denominator_artifact,
    validate_persisted_live_completion_record,
    validate_persisted_live_completion_records,
    validate_persisted_live_runtime_denominator_artifact,
)
from researchops_completion_telemetry.sanitization import (
    CompletionTelemetryError,
    OfflineCompletionRecordBinding,
    build_offline_completion_record,
    sanitize_completion_capture,
    validate_runtime_denominator_artifact,
)
from researchops_completion_telemetry.surface_mapping import (
    load_and_select_surface_mapping,
)
from tests.test_provider_completion_dynamic_artifact_validation import (
    _accepted_artifact,
)
from tests.test_provider_completion_dynamic_denominator import _capture, _plan_binding


ROOT = Path(__file__).resolve().parents[1]
MODULE = ROOT / "src" / "researchops_completion_telemetry" / "persisted_evidence.py"


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


def _plan_snapshot(plan) -> dict[str, object]:
    binding = plan.runtime_binding().runtime_snapshot()
    return {
        "schema_version": "provider-completion-runtime-denominator-plan/1.0",
        **{
            field: binding[field]
            for field in (
                "provider_id",
                "api_surface",
                "transport_id",
                "adapter_version",
                "telemetry_schema_sha256",
                "mapping_schema_version",
                "mapping_version",
                "mapping_sha256",
            )
        },
        "case_ids": list(plan.case_ids),
        "case_ids_sha256": plan.case_ids_sha256,
        "max_turns_per_case": plan.max_turns_per_case,
        "total_model_request_cap": plan.total_model_request_cap,
        "agents_sdk_retries": 0,
        "http_client_retries": 0,
        "denominator_algorithm": plan.denominator_algorithm,
        "exact_response_count_preregistered": False,
    }


class ProviderCompletionPersistedLiveEvidenceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.selection = load_and_select_surface_mapping(
            ROOT,
            "deepseek",
            "responses",
            "openai_compatible_responses",
            purpose="offline_validation",
        )
        cls.expected = {
            "telemetry_schema_sha256": cls.selection.telemetry_schema_sha256,
            "adapter_version": cls.selection.adapter_version,
            "mapping_schema_version": cls.selection.mapping_schema_version,
            "mapping_version": cls.selection.mapping_version,
            "mapping_sha256": cls.selection.mapping_sha256,
            "provider_id": cls.selection.provider_id,
            "api_surface": cls.selection.api_surface,
            "transport_id": cls.selection.transport_id,
            "output_counter_comparability": cls.selection.output_counter_comparability,
            "output_counter_path": cls.selection.output_counter_path,
        }
        cls.context = create_persisted_live_evidence_validation_context(
            cls.selection,
            expected_binding=cls.expected,
        )
        cls.runtime_plan, cls.runtime_artifact = _accepted_artifact()
        cls.plan_snapshot = _plan_snapshot(cls.runtime_plan)
        cls.denominator_context = create_persisted_live_evidence_validation_context(
            cls.selection,
            expected_binding=cls.expected,
            expected_denominator_plan=cls.plan_snapshot,
            expected_denominator_plan_commitment_sha256=(
                cls.runtime_plan.preregistration_commitment
            ),
        )

    def _record(self) -> dict[str, object]:
        raw = {
            "status": "completed",
            "incomplete_details": None,
            "usage": {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
            "provider_request_id": "req_fixture_only",
            "http_status": 200,
            "requested_output_token_cap": 256,
        }
        capture = sanitize_completion_capture(
            raw,
            normalized_usage={
                "requests": 1,
                "input_tokens": 10,
                "output_tokens": 5,
                "total_tokens": 15,
                "cached_input_tokens": 0,
                "cache_write_tokens": None,
                "reasoning_tokens": None,
            },
        )
        binding = OfflineCompletionRecordBinding(
            telemetry_schema_sha256=self.selection.telemetry_schema_sha256,
            adapter_version=self.selection.adapter_version,
            mapping_schema_version=self.selection.mapping_schema_version,
            mapping_version=self.selection.mapping_version,
            mapping_sha256=self.selection.mapping_sha256,
            provider_id=self.selection.provider_id,
            api_surface=self.selection.api_surface,
            transport_id=self.selection.transport_id,
        )

        def resolver(projection, provider_id, api_surface, transport_id):
            self.assertEqual(
                (provider_id, api_surface, transport_id),
                ("deepseek", "responses", "openai_compatible_responses"),
            )
            return self.selection.resolve_mapping(projection)

        record = build_offline_completion_record(
            capture,
            binding=binding,
            response_index=0,
            request_index=0,
            mapping_resolver=resolver,
            output_counter_comparability=self.selection.output_counter_comparability,
            output_counter_path=self.selection.output_counter_path,
        )
        record["record_provenance"] = "live_adapter_write"
        return record

    def test_verified_offline_selection_validates_only_persisted_live_records(self) -> None:
        record = self._record()
        validate_persisted_live_completion_record(record, context=self.context)
        self.assertEqual(
            validate_persisted_live_completion_records(
                [record, copy.deepcopy(record)],
                context=self.context,
            ),
            2,
        )
        offline = copy.deepcopy(record)
        offline["record_provenance"] = "offline_validation"
        with self.assertRaisesRegex(
            CompletionTelemetryError,
            "completion_telemetry_record_provenance_invalid",
        ):
            validate_persisted_live_completion_record(offline, context=self.context)
        historical = copy.deepcopy(record)
        historical["record_provenance"] = "historical_projection"
        with self.assertRaisesRegex(
            CompletionTelemetryError,
            "completion_telemetry_record_provenance_invalid",
        ):
            validate_persisted_live_completion_record(historical, context=self.context)

    def test_binding_mapping_and_privacy_tamper_fail_closed(self) -> None:
        binding = dict(self.expected)
        binding["mapping_sha256"] = "0" * 64
        with self.assertRaisesRegex(
            CompletionTelemetryError,
            "completion_persisted_evidence_binding_mismatch",
        ):
            create_persisted_live_evidence_validation_context(
                self.selection,
                expected_binding=binding,
            )
        extra = dict(self.expected)
        extra["extra"] = None
        with self.assertRaisesRegex(
            CompletionTelemetryError,
            "completion_persisted_evidence_binding_invalid",
        ):
            create_persisted_live_evidence_validation_context(
                self.selection,
                expected_binding=extra,
            )
        record = self._record()
        record["mapping_version"] = "forged"
        with self.assertRaisesRegex(
            CompletionTelemetryError,
            "completion_telemetry_binding_mismatch",
        ):
            validate_persisted_live_completion_record(record, context=self.context)
        with self.assertRaisesRegex(
            CompletionTelemetryError,
            "completion_telemetry_sensitive_value",
        ):
            validate_persisted_live_completion_record(
                self._record(),
                context=self.context,
                sensitive_canaries=("deepseek",),
            )

    def test_runtime_and_persisted_denominator_paths_share_exact_parity(self) -> None:
        self.assertIsNone(
            validate_runtime_denominator_artifact(
                copy.deepcopy(self.runtime_artifact),
                plan_binding=self.runtime_plan,
            )
        )
        self.assertIsNone(
            validate_persisted_live_runtime_denominator_artifact(
                copy.deepcopy(self.runtime_artifact),
                context=self.denominator_context,
            )
        )

        mutations = (
            ("extra", lambda value: value.__setitem__("extra", None)),
            (
                "count",
                lambda value: value.__setitem__(
                    "accepted_response_count",
                    value["accepted_response_count"] - 1,
                ),
            ),
            (
                "cap",
                lambda value: value.__setitem__(
                    "total_model_request_cap",
                    value["total_model_request_cap"] - 1,
                ),
            ),
            (
                "attempt_index",
                lambda value: value["attempts"][0].__setitem__("attempt_index", 9),
            ),
            (
                "case_reconciliation",
                lambda value: value["cases"][0].__setitem__(
                    "sdk_usage_request_reconciliation", "unavailable"
                ),
            ),
            (
                "usage_index_coverage",
                lambda value: value["cases"][0][
                    "sdk_request_usage_indices_by_response"
                ].clear(),
            ),
            (
                "record_binding",
                lambda value: value["records"][0].__setitem__(
                    "mapping_sha256", "0" * 64
                ),
            ),
            (
                "record_index",
                lambda value: value["records"][0].__setitem__("request_index", 1),
            ),
            (
                "offline_provenance",
                lambda value: value["records"][0].__setitem__(
                    "record_provenance", "offline_validation"
                ),
            ),
            (
                "historical_provenance",
                lambda value: value["records"][0].__setitem__(
                    "record_provenance", "historical_projection"
                ),
            ),
        )

        def error_code(callable_object, artifact: dict) -> str:
            with self.assertRaises(CompletionTelemetryError) as caught:
                callable_object(artifact)
            return caught.exception.code

        for label, mutate in mutations:
            with self.subTest(label=label):
                artifact = copy.deepcopy(self.runtime_artifact)
                mutate(artifact)
                runtime_code = error_code(
                    lambda value: validate_runtime_denominator_artifact(
                        value,
                        plan_binding=self.runtime_plan,
                    ),
                    copy.deepcopy(artifact),
                )
                persisted_code = error_code(
                    lambda value: validate_persisted_live_runtime_denominator_artifact(
                        value,
                        context=self.denominator_context,
                    ),
                    copy.deepcopy(artifact),
                )
                self.assertEqual(persisted_code, runtime_code)

    def test_summary_is_frozen_ordinal_only_and_contains_exact_usage(self) -> None:
        artifact = copy.deepcopy(self.runtime_artifact)
        summary = summarize_persisted_live_runtime_denominator_artifact(
            artifact,
            context=self.denominator_context,
        )
        self.assertIsInstance(summary, PersistedRuntimeDenominatorSummary)
        self.assertEqual(
            (
                summary.planned_case_count,
                summary.attempt_count,
                summary.attempts_started,
                summary.attempts_terminal,
                summary.observed_response_count,
                summary.accepted_response_count,
                summary.rejected_response_count,
                summary.not_finalized_case_count,
            ),
            (2, 2, 2, 2, 2, 2, 0, 0),
        )
        self.assertEqual(
            tuple(item.case_ordinal for item in summary.attempts),
            (0, 1),
        )
        self.assertEqual(
            tuple(item.terminal_kind for item in summary.attempts),
            ("response_accepted", "response_accepted"),
        )
        self.assertEqual(
            tuple(item.case_ordinal for item in summary.cases),
            (0, 1),
        )
        self.assertTrue(all(item.closure_eligible for item in summary.cases))
        self.assertEqual(
            (
                summary.cases[0].attempts_started,
                summary.cases[0].attempts_terminal,
                summary.cases[0].observed_response_count,
                summary.cases[0].accepted_response_count,
                summary.cases[0].rejected_response_count,
                summary.cases[0].sdk_raw_response_count,
                summary.cases[0].sdk_raw_response_reconciliation,
                summary.cases[0].sdk_usage_request_count,
                summary.cases[0].sdk_usage_request_reconciliation,
            ),
            (1, 1, 1, 1, 0, 1, "matched", 1, "matched"),
        )
        self.assertIsInstance(
            summary.cases[0].sdk_request_usage_indices_by_response[0],
            PersistedRuntimeUsageIndexSummary,
        )
        self.assertEqual(
            summary.cases[0]
            .sdk_request_usage_indices_by_response[0]
            .sdk_request_usage_indices,
            (0,),
        )
        self.assertEqual(
            tuple(
                (
                    item.response_index,
                    item.request_index,
                    item.normalized_completion_state,
                    item.truncation_signal_source,
                    item.usage_complete,
                    item.requests,
                    item.input_tokens,
                    item.output_tokens,
                    item.total_tokens,
                )
                for item in summary.responses
            ),
            (
                (0, 0, "completed", "native_status", True, 1, 10, 2, 12),
                (1, 1, "completed", "native_status", True, 1, 10, 2, 12),
            ),
        )
        self.assertEqual(
            (
                summary.accepted_record_input_token_total,
                summary.accepted_record_output_token_total,
                summary.accepted_record_total_token_total,
            ),
            (20, 4, 24),
        )
        self.assertTrue(summary.accepted_record_usage_complete)
        self.assertEqual(summary.inner_closure_reasons, ())
        self.assertTrue(summary.inner_closure_claim_allowed)

        projection = asdict(summary)
        rendered = repr(projection)
        for case_id in self.plan_snapshot["case_ids"]:
            self.assertNotIn(case_id, rendered)
        forbidden_keys = {
            "case_id",
            "provider_id",
            "api_surface",
            "transport_id",
            "adapter_version",
            "completion_record",
            "native_status",
            "native_finish_reason",
            "native_stop_reason",
            "native_stop_sequence",
            "native_incomplete_details",
        }

        def assert_projection_safe(value) -> None:
            if isinstance(value, dict):
                self.assertTrue(forbidden_keys.isdisjoint(value))
                for child in value.values():
                    assert_projection_safe(child)
            elif isinstance(value, (list, tuple)):
                for child in value:
                    assert_projection_safe(child)

        assert_projection_safe(projection)
        with self.assertRaises(FrozenInstanceError):
            summary.attempt_count = 99  # type: ignore[misc]
        with self.assertRaises(FrozenInstanceError):
            summary.attempts[0].terminal_kind = "forged"  # type: ignore[misc]
        for summary_type in (
            PersistedRuntimeAttemptSummary,
            PersistedRuntimeUsageIndexSummary,
            PersistedRuntimeCaseSummary,
            PersistedRuntimeResponseSummary,
            PersistedRuntimeDenominatorSummary,
        ):
            with self.subTest(summary_type=summary_type.__name__), self.assertRaises(
                TypeError
            ):
                summary_type()

    def test_summary_uses_defensive_snapshot_not_caller_owned_records(self) -> None:
        artifact = copy.deepcopy(self.runtime_artifact)
        summary = summarize_persisted_live_runtime_denominator_artifact(
            artifact,
            context=self.denominator_context,
        )
        artifact["planned_case_ids"].reverse()
        artifact["attempts"][0]["terminal_kind"] = "outcome_unknown"
        artifact["cases"][0]["closure_eligible"] = False
        artifact["records"][0]["normalized_completion_state"] = "unmapped"
        artifact["records"][0]["usage"]["normalized"]["input_tokens"] = 999_999
        self.assertEqual(summary.attempts[0].terminal_kind, "response_accepted")
        self.assertTrue(summary.cases[0].closure_eligible)
        self.assertEqual(summary.responses[0].normalized_completion_state, "completed")
        self.assertEqual(summary.responses[0].input_tokens, 10)
        self.assertEqual(summary.accepted_record_input_token_total, 20)

    def test_summary_inner_closure_reason_order_matches_runtime_predecessor(self) -> None:
        plan = _plan_binding(
            case_ids=("CASE-001",),
            max_turns=3,
            request_cap=3,
        )
        tracker = capture_module.RuntimeDenominatorTracker(plan)
        accepted = tracker.begin_attempt("CASE-001")
        tracker.finalize_response_accepted(accepted, _capture())
        unknown = tracker.begin_attempt("CASE-001")
        tracker.finalize_outcome_unknown(
            unknown,
            "provider_completion_outcome_unknown",
        )
        no_response = tracker.begin_attempt("CASE-001")
        tracker.finalize_no_response(
            no_response,
            "provider_completion_no_response",
        )
        tracker.seal_case(
            "CASE-001",
            sdk_raw_response_count=1,
            sdk_usage_request_count=3,
            sdk_request_usage_indices_by_response={0: (0, 1, 2)},
        )
        runtime_artifact = tracker.seal_runtime()
        artifact = runtime_artifact.to_dict()
        plan_snapshot = _plan_snapshot(plan)
        context = create_persisted_live_evidence_validation_context(
            self.selection,
            expected_binding=self.expected,
            expected_denominator_plan=plan_snapshot,
            expected_denominator_plan_commitment_sha256=(
                plan.preregistration_commitment
            ),
        )
        summary = summarize_persisted_live_runtime_denominator_artifact(
            artifact,
            context=context,
        )
        predecessor = capture_module.evaluate_runtime_denominator_closure(
            runtime_artifact
        )
        self.assertEqual(
            summary.inner_closure_reasons,
            tuple(predecessor["reasons"]),
        )
        self.assertEqual(
            summary.inner_closure_reasons,
            (
                "model_request_outcome_unknown",
                "request_failed_without_response",
            ),
        )
        self.assertFalse(summary.inner_closure_claim_allowed)
        self.assertTrue(summary.accepted_record_usage_complete)
        self.assertEqual(summary.accepted_record_input_token_total, 10)
        self.assertEqual(summary.attempt_count, 3)
        self.assertEqual(summary.accepted_response_count, 1)
        self.assertEqual(
            tuple(item.terminal_kind for item in summary.attempts),
            ("response_accepted", "outcome_unknown", "no_response"),
        )

    def test_summary_preserves_incomplete_usage_as_null_without_repair(self) -> None:
        plan = _plan_binding(
            case_ids=("CASE-001",),
            max_turns=1,
            request_cap=1,
        )
        tracker = capture_module.RuntimeDenominatorTracker(plan)
        handle = tracker.begin_attempt("CASE-001")
        incomplete_capture = sanitize_completion_capture(
            {
                "status": "completed",
                "incomplete_details": None,
                "usage": {"output_tokens": 2},
                "requested_output_token_cap": 2000,
            },
            normalized_usage={
                "requests": None,
                "input_tokens": None,
                "output_tokens": 2,
                "total_tokens": None,
                "cached_input_tokens": None,
                "cache_write_tokens": None,
                "reasoning_tokens": None,
            },
        )
        tracker.finalize_response_accepted(handle, incomplete_capture)
        tracker.seal_case(
            "CASE-001",
            sdk_raw_response_count=1,
            sdk_usage_request_count=1,
            sdk_request_usage_indices_by_response={0: (0,)},
        )
        artifact = tracker.seal_runtime().to_dict()
        context = create_persisted_live_evidence_validation_context(
            self.selection,
            expected_binding=self.expected,
            expected_denominator_plan=_plan_snapshot(plan),
            expected_denominator_plan_commitment_sha256=(
                plan.preregistration_commitment
            ),
        )
        summary = summarize_persisted_live_runtime_denominator_artifact(
            artifact,
            context=context,
        )
        response = summary.responses[0]
        self.assertFalse(response.usage_complete)
        self.assertFalse(summary.accepted_record_usage_complete)
        self.assertIsNone(response.requests)
        self.assertIsNone(response.input_tokens)
        self.assertEqual(response.output_tokens, 2)
        self.assertIsNone(response.total_tokens)
        self.assertIsNone(summary.accepted_record_input_token_total)
        self.assertIsNone(summary.accepted_record_output_token_total)
        self.assertIsNone(summary.accepted_record_total_token_total)
        # This mirrors the predecessor inner algorithm.  Layer A must add its
        # separate record_usage_incomplete outer gate from the safe response.
        self.assertTrue(summary.inner_closure_claim_allowed)

    def test_persisted_denominator_context_binds_exact_signed_plan_snapshot(
        self,
    ) -> None:
        with self.assertRaisesRegex(
            CompletionTelemetryError,
            "completion_persisted_evidence_plan_required",
        ):
            validate_persisted_live_runtime_denominator_artifact(
                copy.deepcopy(self.runtime_artifact),
                context=self.context,
            )

        cases = []
        extra = copy.deepcopy(self.plan_snapshot)
        extra["extra"] = None
        cases.append((extra, _canonical_sha256(extra), "snapshot_invalid"))
        bad_case_hash = copy.deepcopy(self.plan_snapshot)
        bad_case_hash["case_ids_sha256"] = "0" * 64
        cases.append(
            (bad_case_hash, _canonical_sha256(bad_case_hash), "snapshot_invalid")
        )
        bad_cap = copy.deepcopy(self.plan_snapshot)
        bad_cap["total_model_request_cap"] = 5
        cases.append((bad_cap, _canonical_sha256(bad_cap), "snapshot_invalid"))
        bad_binding = copy.deepcopy(self.plan_snapshot)
        bad_binding["mapping_sha256"] = "0" * 64
        cases.append((bad_binding, _canonical_sha256(bad_binding), "binding_mismatch"))
        cases.append(
            (
                copy.deepcopy(self.plan_snapshot),
                "0" * 64,
                "commitment_mismatch",
            )
        )
        for plan, commitment, suffix in cases:
            with self.subTest(suffix=suffix), self.assertRaises(
                CompletionTelemetryError
            ) as caught:
                create_persisted_live_evidence_validation_context(
                    self.selection,
                    expected_binding=self.expected,
                    expected_denominator_plan=plan,
                    expected_denominator_plan_commitment_sha256=commitment,
                )
            self.assertEqual(
                caught.exception.code,
                "completion_telemetry_runtime_plan_" + suffix,
            )

        with self.assertRaisesRegex(
            CompletionTelemetryError,
            "completion_persisted_evidence_plan_invalid",
        ):
            create_persisted_live_evidence_validation_context(
                self.selection,
                expected_binding=self.expected,
                expected_denominator_plan=self.plan_snapshot,
            )

        caller_owned_plan = copy.deepcopy(self.plan_snapshot)
        copied_context = create_persisted_live_evidence_validation_context(
            self.selection,
            expected_binding=self.expected,
            expected_denominator_plan=caller_owned_plan,
            expected_denominator_plan_commitment_sha256=(
                self.runtime_plan.preregistration_commitment
            ),
        )
        caller_owned_plan["case_ids"].reverse()
        caller_owned_plan["mapping_sha256"] = "0" * 64
        validate_persisted_live_runtime_denominator_artifact(
            copy.deepcopy(self.runtime_artifact),
            context=copied_context,
        )

    def test_persisted_denominator_never_constructs_runtime_capabilities(self) -> None:
        artifact = copy.deepcopy(self.runtime_artifact)
        with (
            patch.object(
                surface_mapping_module.VerifiedSurfaceSelection,
                "create_runtime_binding",
                side_effect=AssertionError("runtime binding constructor called"),
            ),
            patch.object(
                surface_mapping_module.VerifiedRuntimeCompletionBinding,
                "_create",
                side_effect=AssertionError("runtime binding factory called"),
            ),
            patch.object(
                capture_module.VerifiedRuntimeDenominatorPlanBinding,
                "_create",
                side_effect=AssertionError("runtime plan constructor called"),
            ),
            patch.object(
                capture_module.RuntimeDenominatorTracker,
                "__init__",
                side_effect=AssertionError("runtime tracker constructor called"),
            ),
            patch.object(
                capture_module.RuntimeCaseTelemetrySession,
                "__init__",
                side_effect=AssertionError("runtime session constructor called"),
            ),
            patch.object(
                sanitization_module.SanitizedCompletionCapture,
                "__init__",
                side_effect=AssertionError("capture constructor called"),
            ),
        ):
            validate_persisted_live_runtime_denominator_artifact(
                artifact,
                context=self.denominator_context,
            )

    def test_context_is_opaque_immutable_and_has_no_runtime_surface(self) -> None:
        with self.assertRaises(TypeError):
            PersistedLiveEvidenceValidationContext()
        with self.assertRaises(AttributeError):
            self.context.anything = True
        for name in (
            "runtime_binding",
            "runtime_plan",
            "tracker",
            "session",
            "capture",
            "provider",
        ):
            self.assertFalse(hasattr(self.context, name), name)

    def test_module_import_is_pure_and_public_signatures_have_no_seams(self) -> None:
        probe = (
            "import sys\n"
            "import researchops_completion_telemetry.persisted_evidence\n"
            "blocked=('researchops_completion_telemetry.surface_mapping',"
            "'researchops_completion_telemetry.capture','researchops.model_providers',"
            "'researchops.phase6_runner','openai','httpx','agents')\n"
            "print([name for name in blocked if name in sys.modules])\n"
        )
        environment = dict(os.environ)
        environment["PYTHONPATH"] = str(ROOT / "src")
        completed = subprocess.run(
            [sys.executable, "-c", probe],
            cwd=str(ROOT),
            env=environment,
            capture_output=True,
            text=True,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stdout.strip(), "[]")
        for function in (
            create_persisted_live_evidence_validation_context,
            validate_persisted_live_completion_record,
            validate_persisted_live_completion_records,
            validate_persisted_live_runtime_denominator_artifact,
            summarize_persisted_live_runtime_denominator_artifact,
        ):
            parameters = inspect.signature(function).parameters.values()
            self.assertFalse(
                any(item.kind is inspect.Parameter.VAR_KEYWORD for item in parameters)
            )
            forbidden = {"provider", "client", "api_key", "runtime_binding", "tracker", "session"}
            self.assertFalse(forbidden.intersection(inspect.signature(function).parameters))

    def test_source_has_no_network_runtime_dynamic_import_or_write_api(self) -> None:
        tree = ast.parse(MODULE.read_text(encoding="utf-8"))
        imports = {
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        }
        imports.update(
            node.module or ""
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
        )
        blocked = {
            "socket",
            "subprocess",
            "urllib.request",
            "httpx",
            "openai",
            "agents",
            "researchops.model_providers",
            "researchops.audit",
            "researchops.phase6_runner",
            "researchops_completion_telemetry.capture",
        }
        self.assertFalse(imports.intersection(blocked))
        calls = {
            node.func.attr
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        }
        self.assertFalse(
            calls.intersection(
                {"write_text", "write_bytes", "mkdir", "unlink", "rename", "replace"}
            )
        )
        names = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
        self.assertFalse({"eval", "exec", "compile"}.intersection(names))


if __name__ == "__main__":
    unittest.main()
