from __future__ import annotations

import base64
import hashlib
import json
import tempfile
import unittest
from unittest import mock
from pathlib import Path
from typing import Any

from scripts.eval_v2_statistical_compare import (
    ComparisonError,
    _field_rows,
    compare,
)
from scripts.eval_v2_reference_projection import ReferenceProjectionError, build_reference_projection


PROJECT_ROOT = Path(__file__).resolve().parents[1]
PACKAGE_ROOT = PROJECT_ROOT / "evals" / "v2" / "external_review_pre_results_v2"
STAT_ROOT = PACKAGE_ROOT / "statistical_crosscheck"
SHA = "1" * 64
PUBLIC_KEY_B64 = base64.b64encode(bytes(32)).decode("ascii")
SIGNATURE_B64 = base64.b64encode(bytes(64)).decode("ascii")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _read(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def _write(path: Path, value: dict[str, Any]) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")


def _sample_flow(required_columns: list[str]) -> dict[str, Any]:
    return {
        "source_rows": 2,
        "included_rows": 2,
        "excluded_rows": 0,
        "required_columns": required_columns,
        "missing_by_column": {column: 0 for column in required_columns},
        "control_source": 1,
        "control_included": 1,
        "control_excluded": 0,
        "treatment_source": 1,
        "treatment_included": 1,
        "treatment_excluded": 0,
        "realized_population": "intention_to_treat",
    }


def _external_result(package: str, delivery: str, pre_governance: str) -> dict[str, Any]:
    return {
        "schema_version": "1.0",
        "document_type": "independent_statistical_crosscheck_result",
        "package_id": "stat-xcheck-synthetic-trial-pre-results-v1",
        "public_package_commitment_sha256": package,
        "statistical_delivery_commitment_sha256": delivery,
        "pre_governance_commitment_sha256": pre_governance,
        "detached_delivery_manifest_sha256": _sha256(STAT_ROOT / "detached_delivery_manifest.json"),
        "anchor_spec_sha256": _sha256(STAT_ROOT / "anchor_spec.json"),
        "tolerance_policy_sha256": _sha256(STAT_ROOT / "tolerance_policy.json"),
        "field_universe_sha256": _sha256(STAT_ROOT / "comparison_field_universe.json"),
        "execution_id": "RXC-0123456789ABCDEF",
        "executed_at_utc": "2026-08-28T08:00:00Z",
        "input_dataset_sha256": "7ae3c201ccb543b5c647c8c50b2a754294d1d62aaaa458d0f2fb4b0af990ca00",
        "input_design_sha256": "e8ab569e2f877028431d58c3a676d68917d67237303cc194705b5850d400938b",
        "engine": "R",
        "engine_version": "4.4.0",
        "implementation_source_sha256": SHA,
        "runtime_manifest_sha256": SHA,
        "dependency_lock_sha256": SHA,
        "anchor_results": [
            {"anchor_id": "RXC-SF-ANCOVA-001", "values": _sample_flow(["group", "followup_sbp", "baseline_sbp"])},
            {
                "anchor_id": "RXC-ANCOVA-HC3-001",
                "values": {
                    "adjusted_difference": 1.0,
                    "hc3_standard_error": 1.0,
                    "t_statistic": 1.0,
                    "residual_df": 1.0,
                    "two_sided_p_value": 1.0,
                    "confidence_interval_low": 1.0,
                    "confidence_interval_high": 1.0,
                },
            },
            {
                "anchor_id": "RXC-ANCOVA-MEANS-001",
                "values": {
                    "baseline_center": 1.0,
                    "control_adjusted_mean": 1.0,
                    "control_standard_error": 1.0,
                    "control_ci_low": 1.0,
                    "control_ci_high": 1.0,
                    "treatment_adjusted_mean": 1.0,
                    "treatment_standard_error": 1.0,
                    "treatment_ci_low": 1.0,
                    "treatment_ci_high": 1.0,
                },
            },
            {
                "anchor_id": "RXC-ANCOVA-SLOPE-001",
                "values": {
                    "interaction_estimate": 1.0,
                    "hc3_standard_error": 1.0,
                    "t_statistic": 1.0,
                    "residual_df": 1.0,
                    "two_sided_p_value": 1.0,
                    "confidence_interval_low": 1.0,
                    "confidence_interval_high": 1.0,
                },
            },
            {"anchor_id": "RXC-SF-WELCH-001", "values": _sample_flow(["group", "followup_sbp"])},
            {
                "anchor_id": "RXC-WELCH-001",
                "values": {
                    "control_n": 1,
                    "control_mean": 1.0,
                    "control_sample_sd": 1.0,
                    "treatment_n": 1,
                    "treatment_mean": 1.0,
                    "treatment_sample_sd": 1.0,
                    "difference": 1.0,
                    "standard_error": 1.0,
                    "welch_df": 1.0,
                    "t_statistic": 1.0,
                    "two_sided_p_value": 1.0,
                    "confidence_interval_low": 1.0,
                    "confidence_interval_high": 1.0,
                },
            },
            {
                "anchor_id": "RXC-WELCH-HG-001",
                "values": {
                    "pooled_standard_deviation": 1.0,
                    "cohen_d": 1.0,
                    "hedges_exact_correction": 1.0,
                    "hedges_g": 1.0,
                },
            },
            {
                "anchor_id": "RXC-RUNTIME-001",
                "values": {
                    "engine": "R",
                    "engine_version": "4.4.0",
                    "operating_system": "external-os",
                    "locale": "C",
                    "timezone": "UTC",
                    "blas": "external-blas",
                    "lapack": "external-lapack",
                    "thread_count": 1,
                    "implementation_source_sha256": SHA,
                    "runtime_lock_sha256": SHA,
                    "stdout_log_sha256": SHA,
                },
            },
        ],
    }


def _reference_projection(package: str, delivery: str, pre_governance: str) -> dict[str, Any]:
    return build_reference_projection(
        project_root=PROJECT_ROOT,
        package_commitment=package,
        delivery_commitment=delivery,
        pre_governance_commitment=pre_governance,
    )


def _apply_reference_to_external(external: dict[str, Any], reference: dict[str, Any]) -> None:
    universe = _read(STAT_ROOT / "comparison_field_universe.json")
    modes = {field_path: mode for field_path, mode, _ in _field_rows(universe)}
    anchors = {entry["anchor_id"]: entry["values"] for entry in external["anchor_results"]}
    for item in reference["values"]:
        anchor_id, field = item["field_path"].split(".", 1)
        anchors[anchor_id][field] = (
            json.loads(item["canonical_value"])
            if modes[item["field_path"]] == "exact"
            else float(item["canonical_value"])
        )


def _execution_lock(external_path: Path, package: str, delivery: str, pre_governance: str) -> dict[str, Any]:
    external = _read(external_path)
    runtime_target = next(
        entry["values"] for entry in external["anchor_results"] if entry["anchor_id"] == "RXC-RUNTIME-001"
    )
    return {
        "schema_version": "1.0",
        "document_type": "independent_statistical_execution_lock",
        "status": "completed",
        "public_package_commitment_sha256": package,
        "statistical_delivery_commitment_sha256": delivery,
        "pre_governance_commitment_sha256": pre_governance,
        "detached_delivery_manifest_sha256": _sha256(STAT_ROOT / "detached_delivery_manifest.json"),
        "anchor_spec_sha256": _sha256(STAT_ROOT / "anchor_spec.json"),
        "tolerance_policy_sha256": _sha256(STAT_ROOT / "tolerance_policy.json"),
        "result_contract_sha256": _sha256(STAT_ROOT / "result_contract.json"),
        "field_universe_sha256": _sha256(STAT_ROOT / "comparison_field_universe.json"),
        "attempt_ledger_genesis_commitment_sha256": SHA,
        "attempt_sequence": 1,
        "execution_id": "RXC-0123456789ABCDEF",
        "statistical_reviewer_identity_commitment_sha256": SHA,
        "statistical_reviewer_signer": {
            "role": "statistical_reviewer",
            "key_id": "ERS-0000000000000000",
            "public_key_b64": PUBLIC_KEY_B64,
        },
        "engine": "R",
        "engine_version": "4.4.0",
        "implementation_source_sha256": SHA,
        "runtime_manifest_sha256": SHA,
        "dependency_lock_sha256": SHA,
        "stdout_log_sha256": SHA,
        "runtime_target": runtime_target,
        "external_result": {
            "sha256": _sha256(external_path),
            "bytes": external_path.stat().st_size,
            "schema_valid": True,
        },
        "terminal_failure": None,
        "output_lock": {
            "locked_at_utc": "2026-08-28T08:01:00Z",
            "external_timestamp_receipt_sha256": SHA,
            "comparison_had_started": False,
        },
        "prior_public_exposure": {
            "historical_reference_seen": False,
            "python_source_or_tests_seen": False,
            "exposure_note_commitment_sha256": None,
        },
        "claim": {
            "crosscheck_type": "non_blinded_independent_reproducibility",
            "blindness_claim_allowed": False,
            "independent_reimplementation": True,
        },
        "privacy": {
            "raw_rows_included": False,
            "direct_identifiers_included": False,
            "local_paths_included": False,
            "private_content_included": False,
            "provider_or_model_content_included": False,
        },
        "execution_lock_commitment_sha256": SHA,
        "statistical_reviewer_signature": {
            "signed_sha256": SHA,
            "signature_b64": SIGNATURE_B64,
        },
    }


class StatisticalComparatorTests(unittest.TestCase):
    def test_reference_projection_is_deterministic_and_source_bound(self) -> None:
        package = _read(PACKAGE_ROOT / "package_commitments.json")["package_commitment_sha256"]
        delivery = _read(STAT_ROOT / "detached_delivery_manifest.json")["role_delivery_commitment_sha256"]
        first = _reference_projection(package, delivery, "2" * 64)
        second = _reference_projection(package, delivery, "2" * 64)
        self.assertEqual(first, second)
        self.assertEqual(first["value_count"], 64)
        self.assertEqual(len(first["values"]), 64)
        self.assertEqual(len({entry["field_path"] for entry in first["values"]}), 64)
        self.assertEqual(first["reference_generator_sha256"], _sha256(PROJECT_ROOT / "scripts/eval_v2_reference_projection.py"))
        self.assertEqual(first["requirements_lock_sha256"], _sha256(PROJECT_ROOT / "requirements.lock"))

    def test_reference_source_tamper_fails_before_statistical_import(self) -> None:
        package = _read(PACKAGE_ROOT / "package_commitments.json")["package_commitment_sha256"]
        delivery = _read(STAT_ROOT / "detached_delivery_manifest.json")["role_delivery_commitment_sha256"]
        real_sha256 = _sha256

        def tampered(path: Path) -> str:
            if path.name == "analysis_tools.py":
                return "0" * 64
            return real_sha256(path)

        with mock.patch("scripts.eval_v2_reference_projection._sha256", side_effect=tampered):
            with self.assertRaises(ReferenceProjectionError):
                _reference_projection(package, delivery, "2" * 64)

    def test_equal_full_field_universe_produces_75_matches(self) -> None:
        package = _read(PACKAGE_ROOT / "package_commitments.json")["package_commitment_sha256"]
        delivery = _read(STAT_ROOT / "detached_delivery_manifest.json")["role_delivery_commitment_sha256"]
        pre_governance = "2" * 64
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            external_path = root / "external.json"
            reference_path = root / "reference.json"
            lock_path = root / "lock.json"
            output_path = root / "matrix.json"
            external = _external_result(package, delivery, pre_governance)
            reference = _reference_projection(package, delivery, pre_governance)
            _apply_reference_to_external(external, reference)
            _write(external_path, external)
            _write(reference_path, reference)
            _write(lock_path, _execution_lock(external_path, package, delivery, pre_governance))

            result = compare(
                project_root=PROJECT_ROOT,
                execution_lock_path=lock_path,
                external_path=external_path,
                reference_path=reference_path,
                output_path=output_path,
            )
            self.assertEqual(result["status"], "completed")
            self.assertEqual(result["field_count"], 75)
            self.assertEqual(result["matched_count"], 75)
            self.assertEqual(result["discrepancy_count"], 0)
            matrix = _read(output_path)
            self.assertEqual(len(matrix["rows"]), 75)
            self.assertEqual(matrix["summary"]["matched_count"], 75)

    def test_numeric_difference_is_retained_as_discrepancy(self) -> None:
        package = _read(PACKAGE_ROOT / "package_commitments.json")["package_commitment_sha256"]
        delivery = _read(STAT_ROOT / "detached_delivery_manifest.json")["role_delivery_commitment_sha256"]
        pre_governance = "2" * 64
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            external_path = root / "external.json"
            reference_path = root / "reference.json"
            lock_path = root / "lock.json"
            output_path = root / "matrix.json"
            external = _external_result(package, delivery, pre_governance)
            reference = _reference_projection(package, delivery, pre_governance)
            _apply_reference_to_external(external, reference)
            external["anchor_results"][1]["values"]["adjusted_difference"] += 1.0
            _write(external_path, external)
            _write(reference_path, reference)
            _write(lock_path, _execution_lock(external_path, package, delivery, pre_governance))

            result = compare(
                project_root=PROJECT_ROOT,
                execution_lock_path=lock_path,
                external_path=external_path,
                reference_path=reference_path,
                output_path=output_path,
            )
            self.assertEqual(result["discrepancy_count"], 1)
            matrix = _read(output_path)
            discrepant = [row for row in matrix["rows"] if row["status"] != "matched"]
            self.assertEqual(len(discrepant), 1)
            self.assertIsNotNone(discrepant[0]["discrepancy_id"])

    def test_missing_field_and_repository_output_fail_closed(self) -> None:
        package = _read(PACKAGE_ROOT / "package_commitments.json")["package_commitment_sha256"]
        delivery = _read(STAT_ROOT / "detached_delivery_manifest.json")["role_delivery_commitment_sha256"]
        pre_governance = "2" * 64
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            external_path = root / "external.json"
            reference_path = root / "reference.json"
            lock_path = root / "lock.json"
            external = _external_result(package, delivery, pre_governance)
            reference = _reference_projection(package, delivery, pre_governance)
            _apply_reference_to_external(external, reference)
            external["anchor_results"][0]["values"].pop("source_rows")
            _write(external_path, external)
            _write(reference_path, reference)
            _write(lock_path, _execution_lock(external_path, package, delivery, pre_governance))
            with self.assertRaises(ComparisonError):
                compare(
                    project_root=PROJECT_ROOT,
                    execution_lock_path=lock_path,
                    external_path=external_path,
                    reference_path=reference_path,
                    output_path=root / "matrix.json",
                )

            valid_external = _external_result(package, delivery, pre_governance)
            valid_reference = _reference_projection(package, delivery, pre_governance)
            _apply_reference_to_external(valid_external, valid_reference)
            _write(external_path, valid_external)
            _write(lock_path, _execution_lock(external_path, package, delivery, pre_governance))
            _write(reference_path, valid_reference)
            repository_output = PROJECT_ROOT / "artifacts" / "forbidden-stat-matrix.json"
            self.assertFalse(repository_output.exists())
            with self.assertRaises(ComparisonError):
                compare(
                    project_root=PROJECT_ROOT,
                    execution_lock_path=lock_path,
                    external_path=external_path,
                    reference_path=reference_path,
                    output_path=repository_output,
                )
            self.assertFalse(repository_output.exists())

    def test_reference_and_runtime_tampering_fail_closed(self) -> None:
        package = _read(PACKAGE_ROOT / "package_commitments.json")["package_commitment_sha256"]
        delivery = _read(STAT_ROOT / "detached_delivery_manifest.json")["role_delivery_commitment_sha256"]
        pre_governance = "2" * 64
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            external_path = root / "external.json"
            reference_path = root / "reference.json"
            lock_path = root / "lock.json"
            external = _external_result(package, delivery, pre_governance)
            reference = _reference_projection(package, delivery, pre_governance)
            _apply_reference_to_external(external, reference)
            _write(external_path, external)

            tampered_reference = json.loads(json.dumps(reference))
            tampered_reference["values"][0]["canonical_value"] = "999"
            tampered_reference["values"][0]["value_sha256"] = hashlib.sha256(b"999").hexdigest()
            _write(reference_path, tampered_reference)
            _write(lock_path, _execution_lock(external_path, package, delivery, pre_governance))
            with self.assertRaises(ComparisonError) as reference_error:
                compare(
                    project_root=PROJECT_ROOT,
                    execution_lock_path=lock_path,
                    external_path=external_path,
                    reference_path=reference_path,
                    output_path=root / "reference-tamper.json",
                )
            self.assertEqual(reference_error.exception.code, "comparison_reference_projection_mismatch")

            _write(reference_path, reference)
            lock = _execution_lock(external_path, package, delivery, pre_governance)
            lock["runtime_target"]["engine_version"] = "9.9.9"
            _write(lock_path, lock)
            with self.assertRaises(ComparisonError) as runtime_error:
                compare(
                    project_root=PROJECT_ROOT,
                    execution_lock_path=lock_path,
                    external_path=external_path,
                    reference_path=reference_path,
                    output_path=root / "runtime-tamper.json",
                )
            self.assertEqual(runtime_error.exception.code, "comparison_runtime_binding_mismatch")


if __name__ == "__main__":
    unittest.main()
