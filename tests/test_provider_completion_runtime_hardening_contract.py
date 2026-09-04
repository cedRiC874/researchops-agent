from __future__ import annotations

import hashlib
import importlib
import json
import tempfile
import unittest
from pathlib import Path

from researchops.audit import (
    COMPLETION_TELEMETRY_EVENT_SCHEMA_VERSION,
    COMPLETION_TELEMETRY_TERMINAL_EVENT_TYPES,
    COMPLETION_TELEMETRY_UNMAPPED_EVENT,
    AuditLedger,
)
from researchops.completion_telemetry_ledger import (
    LedgerCompletionTelemetrySession,
)
from researchops_completion_telemetry.capture import (
    RuntimeDenominatorTracker,
    evaluate_runtime_denominator_closure,
)
from researchops_completion_telemetry.sanitization import (
    RUNTIME_DENOMINATOR_ARTIFACT_SCHEMA_VERSION,
    sanitize_completion_capture,
    validate_completion_artifact,
    validate_runtime_denominator_artifact,
)
from researchops_completion_telemetry.surface_mapping import (
    SurfaceMappingError,
    load_and_select_surface_mapping,
)
from tests.test_provider_completion_dynamic_denominator import (
    _capture,
    _plan_binding,
)


ROOT = Path(__file__).resolve().parents[1]
CONTRACT_PATH = (
    ROOT
    / "evals"
    / "provider_completion_telemetry_v1"
    / "provider_completion_runtime_hardening_contract_v2.json"
)


def _strict_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _load_contract() -> dict:
    value = json.loads(
        CONTRACT_PATH.read_text(encoding="utf-8"),
        object_pairs_hook=_strict_object,
    )
    if not isinstance(value, dict):
        raise AssertionError("hardening contract must be an object")
    return value


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _resolve_symbol(path: str):
    module_name, name = path.rsplit(".", 1)
    return getattr(importlib.import_module(module_name), name)


def _unknown_capture():
    return sanitize_completion_capture(
        {
            "status": "future_provider_status",
            "incomplete_details": None,
            "usage": {
                "input_tokens": 10,
                "output_tokens": 2,
                "total_tokens": 12,
            },
            "provider_request_id": "req_hardening_contract_unmapped",
            "http_status": 200,
            "requested_output_token_cap": 16,
        },
        normalized_usage={
            "requests": 1,
            "input_tokens": 10,
            "output_tokens": 2,
            "total_tokens": 12,
            "cached_input_tokens": 0,
            "cache_write_tokens": None,
            "reasoning_tokens": None,
        },
    )


class ProviderCompletionRuntimeHardeningContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.contract = _load_contract()

    def test_contract_has_exact_offline_only_successor_shape(self) -> None:
        self.assertEqual(
            set(self.contract),
            {
                "schema_version",
                "contract_id",
                "status",
                "documented_on",
                "predecessors",
                "scope",
                "a1_planned_case_closure",
                "a2_dynamic_denominator_subartifact",
                "a4_unmapped_atomic_terminal",
                "unfinished_prerequisites",
                "claim_boundary",
            },
        )
        self.assertEqual(
            self.contract["schema_version"],
            "provider-completion-runtime-hardening/2.0",
        )
        self.assertEqual(
            self.contract["contract_id"],
            "provider-completion-runtime-hardening-v2",
        )
        self.assertEqual(
            self.contract["status"], "offline_only_hardening_successor"
        )
        self.assertEqual(self.contract["documented_on"], "2026-09-03")
        self.assertEqual(
            self.contract["scope"],
            {
                "hardening_steps": ["A1", "A2", "A4"],
                "online_calls_performed": 0,
                "runtime_authorization": False,
                "provider_runtime_gate_promoted": False,
                "status_defect_state": "open",
                "historical_v1_files_modified": False,
            },
        )
        self.assertEqual(
            self.contract["claim_boundary"],
            {
                "green_ci_closes_defect": False,
                "runtime_execution_authorized": False,
                "provider_registration_authorized": False,
                "model_quality_claim_allowed": False,
                "historical_backfill_performed": False,
            },
        )
        status = (ROOT / "STATUS.md").read_text(encoding="utf-8")
        self.assertIn("状态：`open / cross-cutting / causal attribution incomplete`", status)

    def test_v1_predecessor_bytes_and_hashes_are_immutable(self) -> None:
        predecessors = self.contract["predecessors"]
        self.assertEqual(
            set(predecessors),
            {
                "provider_completion_record_contract_v1",
                "provider_completion_mapping_v1",
            },
        )
        for value in predecessors.values():
            self.assertEqual(
                set(value), {"relative_path", "bytes", "sha256", "immutable"}
            )
            self.assertIs(value["immutable"], True)
            path = (ROOT / value["relative_path"]).resolve()
            self.assertTrue(path.is_relative_to(ROOT.resolve()))
            self.assertTrue(path.is_file())
            self.assertEqual(path.stat().st_size, value["bytes"])
            self.assertEqual(_sha256(path), value["sha256"])

    def test_a1_contract_matches_planned_case_closure_behavior(self) -> None:
        a1 = self.contract["a1_planned_case_closure"]
        self.assertEqual(
            set(a1),
            {
                "implementation_symbol",
                "minimum_per_planned_case",
                "failure_reason",
                "zero_attempt_case_forbids_claim",
                "zero_observed_response_case_forbids_claim",
                "max_turns_is_upper_bound_not_required_count",
            },
        )
        self.assertIs(
            _resolve_symbol(a1["implementation_symbol"]),
            evaluate_runtime_denominator_closure,
        )
        self.assertEqual(
            set(a1["minimum_per_planned_case"]), {"terminal_kind", "count"}
        )
        self.assertEqual(
            a1["minimum_per_planned_case"],
            {"terminal_kind": "response_accepted", "count": 1},
        )
        self.assertEqual(
            a1["failure_reason"], "planned_case_without_accepted_response"
        )
        self.assertIs(a1["zero_attempt_case_forbids_claim"], True)
        self.assertIs(a1["zero_observed_response_case_forbids_claim"], True)
        self.assertIs(a1["max_turns_is_upper_bound_not_required_count"], True)

        failed = RuntimeDenominatorTracker(
            _plan_binding(case_ids=("CASE-001", "CASE-002"), request_cap=2)
        )
        handle = failed.begin_attempt("CASE-001")
        failed.finalize_response_accepted(handle, _capture())
        failed.seal_case(
            "CASE-001", sdk_raw_response_count=1, sdk_usage_request_count=1
        )
        failed.seal_case(
            "CASE-002", sdk_raw_response_count=0, sdk_usage_request_count=0
        )
        closure = evaluate_runtime_denominator_closure(failed.seal_runtime())
        self.assertFalse(closure["claim_allowed"])
        self.assertEqual(closure["reasons"], [a1["failure_reason"]])

        passed = RuntimeDenominatorTracker(
            _plan_binding(
                case_ids=("CASE-001", "CASE-002"),
                max_turns=3,
                request_cap=6,
            )
        )
        for case_id in ("CASE-001", "CASE-002"):
            handle = passed.begin_attempt(case_id)
            passed.finalize_response_accepted(handle, _capture())
            passed.seal_case(
                case_id, sdk_raw_response_count=1, sdk_usage_request_count=1
            )
        self.assertTrue(
            evaluate_runtime_denominator_closure(passed.seal_runtime())[
                "claim_allowed"
            ]
        )

    def test_a2_contract_is_explicitly_denominator_subartifact_only(self) -> None:
        a2 = self.contract["a2_dynamic_denominator_subartifact"]
        self.assertEqual(
            set(a2),
            {
                "schema_version",
                "validator_symbol",
                "compatibility_symbol",
                "scope",
                "revalidates",
                "matched_sdk_response_rows",
                "does_not_validate",
                "full_closure_evidence_verifier_implemented",
            },
        )
        self.assertIs(
            _resolve_symbol(a2["validator_symbol"]),
            validate_runtime_denominator_artifact,
        )
        self.assertIs(
            _resolve_symbol(a2["compatibility_symbol"]),
            validate_completion_artifact,
        )
        self.assertEqual(
            a2["schema_version"], RUNTIME_DENOMINATOR_ARTIFACT_SCHEMA_VERSION
        )
        self.assertEqual(a2["scope"], "runtime_denominator_subartifact_only")
        self.assertEqual(
            a2["matched_sdk_response_rows"],
            "exact_zero_based_coverage_0_to_n_minus_1",
        )
        self.assertEqual(
            a2["revalidates"],
            [
                "plan_binding",
                "attempt_and_response_indices",
                "terminal_counts",
                "case_counts",
                "sdk_reconciliation",
                "matched_sdk_response_row_coverage",
                "live_completion_records",
            ],
        )
        self.assertEqual(
            set(a2["does_not_validate"]),
            {
                "outer_phase6_completion_telemetry_envelope",
                "outer_closure_report",
                "audit_database",
                "audit_index",
                "append_only_event_chain",
                "artifact_manifest",
            },
        )
        self.assertIs(a2["full_closure_evidence_verifier_implemented"], False)
        self.assertIn(
            "runtime denominator subartifact",
            validate_runtime_denominator_artifact.__doc__,
        )

    def test_a4_contract_matches_atomic_unmapped_terminal_and_bijection(self) -> None:
        a4 = self.contract["a4_unmapped_atomic_terminal"]
        self.assertEqual(
            set(a4),
            {
                "ledger_event_schema_version",
                "event_type",
                "terminal_kind",
                "replaces_event_type",
                "accepted_terminal_event_types",
                "additional_side_event_emitted",
                "exactly_one_terminal_event_per_attempt",
                "completion_record_contains_usage",
                "selection_rule",
                "state_event_bijection",
                "recognized_state_event_type",
                "predecessor_v1_record_and_mapping_unchanged",
                "closure_claim_allowed",
                "closure_reason",
                "implementation_symbols",
            },
        )
        self.assertEqual(
            a4["ledger_event_schema_version"],
            COMPLETION_TELEMETRY_EVENT_SCHEMA_VERSION,
        )
        self.assertEqual(a4["event_type"], COMPLETION_TELEMETRY_UNMAPPED_EVENT)
        self.assertIn(
            COMPLETION_TELEMETRY_UNMAPPED_EVENT,
            COMPLETION_TELEMETRY_TERMINAL_EVENT_TYPES,
        )
        self.assertEqual(a4["terminal_kind"], "response_accepted")
        self.assertEqual(
            a4["accepted_terminal_event_types"],
            [
                "model_response_telemetry_recorded",
                COMPLETION_TELEMETRY_UNMAPPED_EVENT,
            ],
        )
        self.assertIs(a4["additional_side_event_emitted"], False)
        self.assertIs(a4["exactly_one_terminal_event_per_attempt"], True)
        self.assertIs(a4["completion_record_contains_usage"], True)
        self.assertEqual(
            a4["selection_rule"],
            "event_type_is_unmapped_iff_normalized_completion_state_is_unmapped",
        )
        self.assertEqual(
            set(a4["state_event_bijection"]),
            {
                "unmapped_state_requires_event_type",
                "event_type_requires_normalized_state",
            },
        )
        self.assertEqual(
            a4["state_event_bijection"],
            {
                "unmapped_state_requires_event_type": COMPLETION_TELEMETRY_UNMAPPED_EVENT,
                "event_type_requires_normalized_state": "unmapped",
            },
        )
        self.assertEqual(
            a4["recognized_state_event_type"],
            "model_response_telemetry_recorded",
        )
        self.assertIs(a4["predecessor_v1_record_and_mapping_unchanged"], True)
        self.assertIs(a4["closure_claim_allowed"], False)
        for symbol in a4["implementation_symbols"]:
            self.assertIsNotNone(_resolve_symbol(symbol))

        plan = _plan_binding(
            case_ids=("CASE-UNMAPPED-001",), max_turns=1, request_cap=1
        )
        tracker = RuntimeDenominatorTracker(plan)
        with tempfile.TemporaryDirectory() as directory:
            ledger = AuditLedger(Path(directory) / "audit.sqlite3")
            run_id = ledger.start_run(
                mode="hardening-contract-unmapped",
                request_summary={"objective": "offline contract replay"},
            )
            bridge = LedgerCompletionTelemetrySession(
                tracker.bind_case("CASE-UNMAPPED-001"),
                ledger=ledger,
                run_id=run_id,
                runtime_plan_binding=plan,
            )
            bridge.finalize_response_accepted(
                bridge.begin_attempt(), _unknown_capture()
            )
            terminals = [
                event
                for event in ledger.export_run(run_id)["events"]
                if event["event_type"] in COMPLETION_TELEMETRY_TERMINAL_EVENT_TYPES
            ]
        self.assertEqual(len(terminals), 1)
        self.assertEqual(terminals[0]["event_type"], a4["event_type"])
        record = terminals[0]["safe_payload"]["completion_record"]
        self.assertEqual(record["normalized_completion_state"], "unmapped")
        self.assertIn("usage", record)
        self.assertNotEqual(terminals[0]["event_type"], a4["replaces_event_type"])

        tracker.seal_case(
            "CASE-UNMAPPED-001",
            sdk_raw_response_count=1,
            sdk_usage_request_count=1,
            sdk_request_usage_indices_by_response={0: (0,)},
        )
        closure = evaluate_runtime_denominator_closure(tracker.seal_runtime())
        self.assertFalse(closure["claim_allowed"])
        self.assertIn(a4["closure_reason"], closure["reasons"])

    def test_unfinished_a3_and_registry_promotion_keep_runtime_blocked(self) -> None:
        self.assertEqual(
            self.contract["unfinished_prerequisites"],
            {
                "a3_external_preregistration_binding": "not_implemented",
                "adapter_path_first_live_validation": "not_performed",
                "registry_successor_runtime_promotion": "not_implemented",
                "full_outer_closure_ledger_verifier": "not_implemented",
            },
        )
        registry = json.loads(
            (
                ROOT
                / "evals"
                / "provider_completion_telemetry_v2"
                / "provider_completion_surface_registry_v2.json"
            ).read_text(encoding="utf-8")
        )
        self.assertTrue(registry["entries"])
        self.assertTrue(
            all(entry["runtime_binding_allowed"] is False for entry in registry["entries"])
        )
        self.assertTrue(
            all(entry["first_live_validation_required"] is True for entry in registry["entries"])
        )
        with self.assertRaises(SurfaceMappingError) as blocked:
            load_and_select_surface_mapping(
                ROOT,
                "deepseek",
                "responses",
                "openai_compatible_responses",
                purpose="runtime_binding",
            )
        self.assertEqual(
            blocked.exception.code, "surface_mapping_runtime_binding_blocked"
        )


if __name__ == "__main__":
    unittest.main()
