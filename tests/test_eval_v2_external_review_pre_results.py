from __future__ import annotations

import hashlib
import json
import re
import unittest
from collections import Counter
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator


PROJECT_ROOT = Path(__file__).resolve().parents[1]
PACKAGE_ROOT = PROJECT_ROOT / "evals" / "v2" / "external_review_pre_results_v2"
COMMITMENTS_PATH = PACKAGE_ROOT / "package_commitments.json"
RUNBOOK_PATH = PROJECT_ROOT / "docs" / "EVAL_V2_EXTERNAL_REVIEW_PREP_V2.md"

BASE_COMMIT = "b905477449938b471c4b9af84398ad6e7ba2212b"
BASE_TREE = "5b0be18e61bc14dd255e56f775e278e83ffad094"
PACKAGE_DOMAIN = "researchops-external-review-package-v2"
PACKAGE_COMMITMENT = "2debdcd1848f4fa22e9ee2b35cf337590718a8063cfe845c75c40993f676f5be"

EXPECTED_SCENARIOS = {
    "approval_pause": 6,
    "clarification_required": 22,
    "duplicate_tool_call": 12,
    "output_truncation": 6,
    "prompt_injection": 11,
    "provider_timeout": 6,
    "safe_refusal": 19,
    "side_effect_outcome_unknown": 6,
    "standard_analysis": 20,
    "unauthorized_resource": 12,
}

EXPECTED_DATASETS = {
    "palmer_penguins_v0_1_0": {"total": 40, "development": 27, "public_regression": 13},
    "uci_parkinsons_telemonitoring_189": {"total": 40, "development": 27, "public_regression": 13},
    "uci_heart_disease_cleveland_45": {"total": 40, "development": 26, "public_regression": 14},
}

EXPECTED_ANCHORS = [
    "RXC-SF-ANCOVA-001",
    "RXC-ANCOVA-HC3-001",
    "RXC-ANCOVA-MEANS-001",
    "RXC-ANCOVA-SLOPE-001",
    "RXC-SF-WELCH-001",
    "RXC-WELCH-001",
    "RXC-WELCH-HG-001",
    "RXC-RUNTIME-001",
]

EXPECTED_REVIEW_AXES = [
    "dataset_and_design_boundary",
    "expected_outcome",
    "tool_sequence_and_arguments",
    "numeric_claim_direction_and_tolerance",
    "evidence_requirements",
    "missingness_and_repeated_measure_handling",
    "observational_and_causal_language",
    "approval_and_safety_boundary",
    "required_and_forbidden_assertions",
]

EXPECTED_INPUT_PATHS = {
    "campaign": "evals/v2/campaign.json",
    "dataset_manifest": "evals/v2/external_datasets.json",
    "public_corpus": "evals/v2/public_tasks.jsonl",
    "public_task_schema": "evals/v2/public_task_schema.json",
    "internal_review": "evals/v2/internal_review.json",
}

EXPECTED_SCORER_PATHS = [
    "src/researchops/eval_v2_contracts.py",
    "src/researchops/eval_v2_public.py",
    "src/researchops/eval_v2_runner.py",
]


def _reject_constant(value: str) -> Any:
    raise ValueError(f"non-finite JSON constant: {value}")


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _load_json(path: Path) -> dict[str, Any]:
    value = json.loads(
        path.read_text(encoding="utf-8"),
        parse_constant=_reject_constant,
        object_pairs_hook=_reject_duplicate_keys,
    )
    if not isinstance(value, dict):
        raise TypeError(f"expected object: {path}")
    return value


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _git_blob_sha(path: Path) -> str:
    content = path.read_bytes()
    return hashlib.sha1(b"blob " + str(len(content)).encode("ascii") + b"\0" + content).hexdigest()


def _sha256_bundle_v1(paths: list[Path]) -> str:
    digest = hashlib.sha256()
    relative_paths = sorted(path.relative_to(PROJECT_ROOT).as_posix() for path in paths)
    for relative in relative_paths:
        content = (PROJECT_ROOT / relative).read_bytes()
        path_bytes = relative.encode("utf-8")
        digest.update(len(path_bytes).to_bytes(8, "big"))
        digest.update(path_bytes)
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
    return digest.hexdigest()


class ExternalReviewPreResultsPackageTests(unittest.TestCase):
    def test_all_machine_schemas_are_valid_draft_2020_12(self) -> None:
        for relative in (
            "domain_review_record.schema.json",
            "governance_receipt.schema.json",
            "statistical_crosscheck/result_contract.json",
        ):
            Draft202012Validator.check_schema(_load_json(PACKAGE_ROOT / relative))

    def test_package_commitment_binds_every_payload_file(self) -> None:
        commitments = _load_json(COMMITMENTS_PATH)
        self.assertEqual(commitments["status"], "preparation_only_not_evidence")
        self.assertEqual(commitments["base_commit_sha"], BASE_COMMIT)
        self.assertEqual(commitments["base_tree_sha"], BASE_TREE)
        self.assertEqual(commitments["commitment_domain"], PACKAGE_DOMAIN)
        self.assertTrue(commitments["commitment_file_excluded_from_preimage"])

        expected_paths = {RUNBOOK_PATH.relative_to(PROJECT_ROOT).as_posix()}
        expected_paths.update(
            path.relative_to(PROJECT_ROOT).as_posix()
            for path in PACKAGE_ROOT.rglob("*")
            if path.is_file() and path != COMMITMENTS_PATH
        )
        entries = commitments["files"]
        self.assertEqual(commitments["file_count"], len(expected_paths))
        self.assertEqual({entry["path"] for entry in entries}, expected_paths)

        preimage = f"{PACKAGE_DOMAIN}\n"
        for entry in sorted(entries, key=lambda item: item["path"]):
            path = (PROJECT_ROOT / entry["path"]).resolve()
            self.assertTrue(path.is_relative_to(PROJECT_ROOT.resolve()))
            self.assertEqual(path.stat().st_size, entry["bytes"])
            self.assertEqual(_sha256(path), entry["sha256"])
            preimage += f'{entry["path"]}\t{entry["bytes"]}\t{entry["sha256"]}\n'

        actual = hashlib.sha256(preimage.encode("utf-8")).hexdigest()
        self.assertEqual(actual, PACKAGE_COMMITMENT)
        self.assertEqual(commitments["package_commitment_sha256"], PACKAGE_COMMITMENT)
        self.assertFalse(commitments["external_review_completed"])
        self.assertFalse(commitments["statistical_crosscheck_completed"])
        self.assertEqual(commitments["network_calls"], 0)

    def test_content_scope_is_full_candidate_and_provider_neutral_corpus(self) -> None:
        manifest = _load_json(PACKAGE_ROOT / "content_review_manifest.json")
        self.assertEqual(manifest["status"], "frozen_scope_not_review_evidence")
        self.assertEqual(manifest["base_commit_sha"], BASE_COMMIT)
        self.assertEqual(manifest["base_tree_sha"], BASE_TREE)
        self.assertTrue(manifest["candidate_neutral"])
        self.assertTrue(manifest["provider_neutral"])
        self.assertFalse(manifest["model_or_provider_results_included"])
        self.assertFalse(manifest["private_content_included"])
        self.assertFalse(manifest["external_review_completed"])
        self.assertEqual(
            set(manifest),
            {
                "schema_version",
                "manifest_id",
                "status",
                "base_commit_sha",
                "base_tree_sha",
                "candidate_neutral",
                "provider_neutral",
                "inputs",
                "control_plane_bindings",
                "selection",
                "dataset_counts",
                "scenario_counts",
                "review_axes",
                "model_or_provider_results_included",
                "private_content_included",
                "external_review_completed",
            },
        )
        self.assertEqual(set(manifest["inputs"]), set(EXPECTED_INPUT_PATHS))
        self.assertEqual(
            {name: entry["path"] for name, entry in manifest["inputs"].items()},
            EXPECTED_INPUT_PATHS,
        )

        for entry in manifest["inputs"].values():
            path = PROJECT_ROOT / entry["path"]
            self.assertEqual(path.stat().st_size, entry["bytes"])
            self.assertEqual(_sha256(path), entry["sha256"])

        tasks = [
            json.loads(
                line,
                parse_constant=_reject_constant,
                object_pairs_hook=_reject_duplicate_keys,
            )
            for line in (PROJECT_ROOT / "evals" / "v2" / "public_tasks.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()
            if line
        ]
        self.assertEqual(len(tasks), 120)
        self.assertEqual(len({task["task_id"] for task in tasks}), 120)
        self.assertTrue(all(task["lifecycle_status"] == "ready" for task in tasks))
        self.assertTrue(all(task["review_status"] == "internal_reviewed" for task in tasks))

        selection = manifest["selection"]
        self.assertEqual(selection["method"], "all_tasks_in_bound_public_corpus_in_file_order")
        self.assertFalse(selection["sampling_performed"])
        self.assertIsNone(selection["seed"])
        self.assertEqual(selection["task_count"], 120)
        self.assertEqual(selection["unique_task_id_count"], 120)
        self.assertEqual(selection["development_count"], 80)
        self.assertEqual(selection["public_regression_count"], 40)
        self.assertEqual(selection["reviewed_by_each_domain_reviewer_target"], 120)
        self.assertEqual(Counter(task["split"] for task in tasks), {"development": 80, "public_regression": 40})
        self.assertEqual(Counter(task["scenario"] for task in tasks), EXPECTED_SCENARIOS)
        self.assertEqual(manifest["scenario_counts"], EXPECTED_SCENARIOS)
        self.assertEqual(manifest["review_axes"], EXPECTED_REVIEW_AXES)

        source_manifest = _load_json(PROJECT_ROOT / "evals" / "v2" / "external_datasets.json")
        source_datasets = {entry["dataset_id"]: entry for entry in source_manifest["datasets"]}
        self.assertEqual(set(manifest["dataset_counts"]), set(EXPECTED_DATASETS))

        for dataset_id, expected in EXPECTED_DATASETS.items():
            selected = [task for task in tasks if task["dataset_id"] == dataset_id]
            self.assertEqual(len(selected), expected["total"])
            self.assertEqual(Counter(task["split"] for task in selected)["development"], expected["development"])
            self.assertEqual(
                Counter(task["split"] for task in selected)["public_regression"],
                expected["public_regression"],
            )
            declared = manifest["dataset_counts"][dataset_id]
            source = source_datasets[dataset_id]
            self.assertEqual(declared["task_count"], expected["total"])
            self.assertEqual(declared["development_count"], expected["development"])
            self.assertEqual(declared["public_regression_count"], expected["public_regression"])
            self.assertEqual(declared["domain"], source["domain"])
            self.assertEqual(declared["asset_sha256"], source["source"]["selected_asset_sha256"])
            self.assertEqual(declared["asset_bytes"], source["source"]["selected_asset_bytes"])
            self.assertEqual(declared["rows"], source["structure"]["row_count"])
            self.assertEqual(declared["columns"], source["structure"]["column_count"])
            self.assertEqual(declared["missing_cells"], source["structure"]["missing_cell_count"])
            self.assertEqual(declared["license"], source["license"]["identifier"])
            if source["structure"]["subject_count"] is not None:
                self.assertEqual(declared["subject_count"], source["structure"]["subject_count"])
        self.assertTrue(
            manifest["dataset_counts"]["uci_parkinsons_telemonitoring_189"][
                "subject_identifier_must_not_enter_review_output"
            ]
        )

        controls = manifest["control_plane_bindings"]
        self.assertEqual(
            set(controls),
            {
                "tool_contract_sha256",
                "scorer_bundle_sha256",
                "split_manifest_sha256",
                "tool_contract",
                "split_manifest",
                "scorer_bundle",
                "candidate_selection_or_result_included",
            },
        )
        for name in ("tool_contract", "split_manifest"):
            entry = controls[name]
            path = PROJECT_ROOT / entry["path"]
            self.assertEqual(path.stat().st_size, entry["bytes"])
            self.assertEqual(_sha256(path), entry["sha256"])
        self.assertEqual(controls["tool_contract_sha256"], controls["tool_contract"]["sha256"])
        self.assertEqual(controls["split_manifest_sha256"], controls["split_manifest"]["sha256"])

        scorer_entries = controls["scorer_bundle"]["paths"]
        self.assertEqual([entry["path"] for entry in scorer_entries], EXPECTED_SCORER_PATHS)
        scorer_paths = []
        for entry in scorer_entries:
            path = PROJECT_ROOT / entry["path"]
            scorer_paths.append(path)
            self.assertEqual(path.stat().st_size, entry["bytes"])
            self.assertEqual(_sha256(path), entry["sha256"])
        self.assertEqual(
            controls["scorer_bundle"]["algorithm"],
            "sha256_bundle_v1_sorted_posix_path_with_u64be_path_length_path_u64be_content_length_content",
        )
        self.assertEqual(_sha256_bundle_v1(scorer_paths), controls["scorer_bundle_sha256"])
        self.assertFalse(controls["candidate_selection_or_result_included"])
        self.assertTrue(manifest["inputs"]["internal_review"]["bound_for_provenance"])
        self.assertFalse(manifest["inputs"]["internal_review"]["decisions_disclosed_to_external_reviewers"])

    def test_protocol_closes_reviewer_shopping_time_and_claim_gaps(self) -> None:
        protocol = _load_json(PACKAGE_ROOT / "protocol.json")
        self.assertEqual(protocol["status"], "preparation_only_not_evidence")
        self.assertEqual(protocol["strict_time_comparator"], "less_than_only")
        self.assertTrue(protocol["external_timestamp_anchor_required"])

        scope = protocol["content_review_scope"]
        self.assertFalse(scope["candidate_bound"])
        self.assertFalse(scope["provider_bound"])
        self.assertEqual(scope["reviewed_task_target"], 120)
        self.assertEqual(scope["minimum_independent_domain_reviewers"], 2)
        self.assertTrue(scope["each_reviewer_reviews_full_scope"])
        self.assertFalse(scope["candidate_provider_results_disclosed"])

        governance = protocol["reviewer_governance"]
        self.assertTrue(governance["roster_must_be_externally_anchored_before_invitation"])
        self.assertTrue(governance["all_invitation_outcomes_must_be_committed"])
        self.assertFalse(governance["publish_only_approved_receipts_allowed"])
        self.assertTrue(governance["replacement_requires_new_versioned_roster"])
        self.assertTrue(governance["cross_role_identity_separation_required"])
        self.assertEqual(len(governance["allowed_invitation_outcomes"]), 6)

        time_order = protocol["strict_time_order"]
        crosscheck_order = protocol["statistical_crosscheck"]["strict_time_order"]
        self.assertEqual(len(time_order), len(set(time_order)))
        self.assertEqual(len(crosscheck_order), len(set(crosscheck_order)))
        self.assertLess(time_order.index("reviewer_roster_anchor_at"), time_order.index("first_invitation_sent_at"))
        self.assertEqual(time_order[-1], "future_model_results_start_at")
        self.assertEqual(crosscheck_order[-1], "future_model_results_start_at")

        crosscheck = protocol["statistical_crosscheck"]
        self.assertFalse(crosscheck["blindness_claim_allowed"])
        self.assertTrue(crosscheck["non_blinded_independent_reproducibility_claim_allowed"])
        self.assertTrue(crosscheck["prior_exposure_must_be_disclosed"])

        completion = protocol["domain_review_completion_gate"]
        self.assertEqual(completion["each_record_exact_unique_bound_task_ids"], 120)
        self.assertEqual(completion["each_record_exact_unique_bound_dataset_ids"], 3)
        self.assertEqual(completion["every_task_overall_decision_must_equal"], "approve")
        self.assertEqual(completion["every_task_axis_decision_must_equal"], "approve")
        self.assertEqual(completion["needs_revision_count_must_equal"], 0)
        self.assertEqual(completion["reject_count_must_equal"], 0)
        self.assertEqual(completion["out_of_scope_count_must_equal"], 0)
        self.assertEqual(completion["cross_reviewer_disagreement_count_must_equal"], 0)
        self.assertTrue(completion["negative_or_incomplete_record_commitments_must_be_preserved"])

        record_schema = _load_json(PROJECT_ROOT / completion["record_schema"])
        task_schema = record_schema["$defs"]["task_review"]
        self.assertIn("overall_decision", task_schema["required"])
        self.assertEqual(record_schema["properties"]["task_reviews"]["minItems"], 120)
        self.assertEqual(record_schema["properties"]["task_reviews"]["maxItems"], 120)
        record_commitment = record_schema["x-researchops-record-commitment"]
        self.assertEqual(record_commitment["canonicalization"], "RFC8785_JCS")
        self.assertEqual(record_commitment["self_field_excluded"], "record_commitment_sha256")

        governance_schema = _load_json(PACKAGE_ROOT / "governance_receipt.schema.json")
        self.assertIn("invitation_ledger_commitment_sha256", governance_schema["required"])
        self.assertIn("role_identity_commitments", governance_schema["required"])
        self.assertIn("time_anchors", governance_schema["required"])
        governance_commitment = governance_schema["x-researchops-document-commitment-and-signature"]
        self.assertEqual(governance_commitment["canonicalization"], "RFC8785_JCS")
        self.assertEqual(
            governance_commitment["excluded_fields"],
            ["document_commitment_sha256", "governance_signature"],
        )
        self.assertEqual(governance_commitment["signed_sha256_must_equal"], "document_commitment_sha256")

        self.assertTrue(all(value is False for value in protocol["claim_boundaries"].values()))
        self.assertEqual(protocol["network_calls"], 0)

    def test_crosscheck_is_empty_independent_specification(self) -> None:
        spec = _load_json(PACKAGE_ROOT / "statistical_crosscheck" / "anchor_spec.json")
        tolerance = _load_json(PACKAGE_ROOT / "statistical_crosscheck" / "tolerance_policy.json")
        result_contract = _load_json(PACKAGE_ROOT / "statistical_crosscheck" / "result_contract.json")

        self.assertEqual(spec["status"], "specification_only_not_run")
        self.assertFalse(spec["candidate_bound"])
        self.assertFalse(spec["provider_bound"])
        self.assertFalse(spec["crosscheck_completed"])
        self.assertFalse(spec["network_allowed"])
        self.assertFalse(spec["python_allowed"])
        self.assertTrue(all(value is False for value in spec["delivery_boundary"].values()))
        self.assertFalse(spec["independence_claims"]["blindness_claim_allowed"])
        self.assertTrue(spec["independence_claims"]["non_blinded_independent_reproducibility_claim_allowed"])
        sample_flow = spec["sample_flow_semantics"]
        self.assertEqual(sample_flow["unexpected_nonmissing_group_label"], "fail_closed_not_excluded")
        self.assertEqual(
            sample_flow["missing_group_label"],
            "method_specific_required_field_missing_and_available_case_excluded",
        )

        for entry in spec["inputs"].values():
            path = PROJECT_ROOT / entry["repository_source"]
            self.assertEqual(path.stat().st_size, entry["bytes"])
            self.assertEqual(_sha256(path), entry["sha256"])
            self.assertEqual(_git_blob_sha(path), entry["git_blob"])

        anchor_ids = [entry["anchor_id"] for entry in spec["anchors"]]
        self.assertEqual(anchor_ids, EXPECTED_ANCHORS)
        result_refs = result_contract["properties"]["anchor_results"]["prefixItems"]
        result_anchor_ids = [
            result_contract["$defs"][entry["$ref"].split("/")[-1]]["properties"]["anchor_id"]["const"]
            for entry in result_refs
        ]
        self.assertEqual(result_anchor_ids, EXPECTED_ANCHORS)
        self.assertFalse(result_contract["additionalProperties"])
        for required in (
            "public_package_commitment_sha256",
            "detached_delivery_manifest_sha256",
            "detached_bundle_commitment_sha256",
            "anchor_spec_sha256",
            "tolerance_policy_sha256",
            "result_commitment_sha256",
        ):
            self.assertIn(required, result_contract["required"])
        commitment_policy = result_contract["x-researchops-result-commitment"]
        self.assertEqual(commitment_policy["canonicalization"], "RFC8785_JCS")
        self.assertEqual(commitment_policy["self_field_excluded"], "result_commitment_sha256")

        self.assertFalse(tolerance["post_result_tolerance_change_allowed"])
        self.assertFalse(tolerance["round_before_compare_allowed"])
        self.assertFalse(tolerance["non_finite_values_allowed"])
        self.assertTrue(tolerance["failed_version_must_be_preserved"])
        self.assertTrue(tolerance["repair_requires_new_version"])

        numeric_anchors = {
            entry["anchor_id"]: entry["required_outputs"]
            for entry in spec["anchors"]
            if entry["anchor_id"] not in {"RXC-SF-ANCOVA-001", "RXC-SF-WELCH-001", "RXC-RUNTIME-001"}
        }
        numeric_fields = {
            f"{anchor_id}.{field}"
            for anchor_id, fields in numeric_anchors.items()
            for field in fields
        }
        exact_numeric = set(tolerance["exact_numeric_fields"])
        self.assertEqual(set(tolerance["field_tolerance_map"]) | exact_numeric, numeric_fields)
        self.assertEqual(set(tolerance["field_tolerance_map"]) & exact_numeric, set())
        self.assertEqual(
            set(tolerance["field_tolerance_map"].values()),
            {"estimate_mean_sd_se_t_df_ci_center_effect_size", "p_value"},
        )

        executable_suffixes = {".r", ".sas", ".py"}
        unexpected = [
            path
            for path in (PACKAGE_ROOT / "statistical_crosscheck").iterdir()
            if path.suffix.lower() in executable_suffixes
        ]
        self.assertEqual(unexpected, [])
        attestation = (
            PACKAGE_ROOT
            / "statistical_crosscheck"
            / "runtime_and_independence_attestation.template.md"
        ).read_text(encoding="utf-8")
        self.assertNotIn("before reference outputs are revealed", attestation)
        self.assertIn("before comparison and output evidence binding", attestation)

    def test_detached_crosscheck_manifest_is_independently_recomputable(self) -> None:
        path = PACKAGE_ROOT / "statistical_crosscheck" / "detached_delivery_manifest.json"
        manifest = _load_json(path)
        self.assertEqual(manifest["status"], "frozen_delivery_not_run")
        self.assertEqual(manifest["base_commit_sha"], BASE_COMMIT)
        self.assertEqual(manifest["base_tree_sha"], BASE_TREE)
        self.assertFalse(manifest["blindness_claim_allowed"])
        self.assertFalse(manifest["crosscheck_completed"])
        self.assertEqual(manifest["network_calls"], 0)

        entries = manifest["files"]
        self.assertEqual(manifest["file_count"], len(entries))
        self.assertEqual(len({entry["delivery_name"] for entry in entries}), len(entries))
        preimage = f'{manifest["commitment_domain"]}\n'
        for entry in sorted(entries, key=lambda item: item["delivery_name"]):
            source = PROJECT_ROOT / entry["repository_source"]
            self.assertEqual(source.stat().st_size, entry["bytes"])
            self.assertEqual(_sha256(source), entry["sha256"])
            preimage += f'{entry["delivery_name"]}\t{entry["bytes"]}\t{entry["sha256"]}\n'
        actual = hashlib.sha256(preimage.encode("utf-8")).hexdigest()
        self.assertEqual(actual, manifest["detached_bundle_commitment_sha256"])

        package_commitments = _load_json(COMMITMENTS_PATH)
        public_entry = next(
            entry
            for entry in package_commitments["files"]
            if entry["path"].endswith("statistical_crosscheck/detached_delivery_manifest.json")
        )
        self.assertEqual(public_entry["sha256"], _sha256(path))

    def test_package_contains_no_direct_identity_secret_or_completed_receipt(self) -> None:
        texts = [path.read_text(encoding="utf-8") for path in PACKAGE_ROOT.rglob("*") if path.is_file()]
        texts.append(RUNBOOK_PATH.read_text(encoding="utf-8"))
        combined = "\n".join(texts)

        self.assertIsNone(re.search(r"(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", combined))
        self.assertIsNone(re.search(r"(?i)\bsk-(?:ant-)?[A-Za-z0-9_-]{16,}\b", combined))
        self.assertIsNone(re.search(r"(?i)Bearer\s+[A-Za-z0-9._~+/-]{12,}", combined))
        self.assertIsNone(re.search(r"(?i)[A-Z]:\\Users\\", combined))

        filenames = {path.name.lower() for path in PACKAGE_ROOT.rglob("*") if path.is_file()}
        self.assertFalse(any(name.endswith(".receipt.json") for name in filenames))
        self.assertFalse(any(name.endswith(".output.json") for name in filenames))


if __name__ == "__main__":
    unittest.main()
