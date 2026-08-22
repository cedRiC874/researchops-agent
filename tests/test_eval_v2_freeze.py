from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from researchops.eval_v2_contracts import EvalV2ContractError
from researchops.eval_v2_freeze import (
    candidate_commitment_sha256,
    load_public_regression_task_orders,
    sha256_bundle_v1,
    validate_public_regression_candidate,
)


REPO_ROOT = Path(__file__).resolve().parents[1]
CANDIDATE_PATH = REPO_ROOT / "evals" / "v2" / "public_regression_candidate.json"
SPLIT_PATH = REPO_ROOT / "evals" / "v2" / "public_regression_split_manifest.json"


class EvalV2FreezeTests(unittest.TestCase):
    def test_repository_public_regression_candidate_is_locked_but_not_full_freeze(self) -> None:
        result = validate_public_regression_candidate(
            project_root=REPO_ROOT,
            candidate_path=CANDIDATE_PATH,
            verify_environment=True,
        )

        self.assertEqual(result["status"], "valid")
        self.assertEqual(result["candidate_status"], "candidate_locked")
        self.assertFalse(result["full_campaign_frozen"])
        self.assertFalse(result["private_holdout_access_authorized"])
        self.assertFalse(result["model_quality_claim_allowed"])
        self.assertEqual(result["provider_behavior_task_count"], 31)
        self.assertEqual(result["deterministic_fault_injection_task_count"], 9)
        self.assertEqual(result["dependency_lock"]["exact_pin_count"], 58)
        self.assertFalse(result["dependency_lock"]["artifact_hashes_included"])

    def test_precommitted_orders_are_distinct_and_filter_by_execution_channel(self) -> None:
        all_orders = load_public_regression_task_orders(SPLIT_PATH)
        provider_orders = load_public_regression_task_orders(
            SPLIT_PATH, execution_channel="provider_behavior"
        )
        fault_orders = load_public_regression_task_orders(
            SPLIT_PATH, execution_channel="deterministic_fault_injection"
        )

        self.assertEqual(set(all_orders), {1, 2, 3})
        self.assertEqual({len(value) for value in all_orders.values()}, {40})
        self.assertEqual({len(value) for value in provider_orders.values()}, {31})
        self.assertEqual({len(value) for value in fault_orders.values()}, {9})
        self.assertEqual(len(set(all_orders.values())), 3)
        for index in (1, 2, 3):
            self.assertEqual(
                set(provider_orders[index]).union(fault_orders[index]),
                set(all_orders[index]),
            )

    def test_component_drift_fails_even_with_recomputed_commitment(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            payload = json.loads(CANDIDATE_PATH.read_text(encoding="utf-8"))
            payload["component_hashes"]["prompt_source_sha256"] = "0" * 64
            payload["candidate_commitment_sha256"] = candidate_commitment_sha256(payload)
            path = Path(temporary) / "candidate.json"
            path.write_text(json.dumps(payload), encoding="utf-8")

            with self.assertRaises(EvalV2ContractError) as caught:
                validate_public_regression_candidate(
                    project_root=REPO_ROOT,
                    candidate_path=path,
                )

        self.assertEqual(
            caught.exception.code, "eval_v2_public_candidate_component_drift"
        )

    def test_bundle_hash_rejects_escape_and_duplicate_scope(self) -> None:
        with self.assertRaises(EvalV2ContractError) as escape:
            sha256_bundle_v1(REPO_ROOT, ("../outside.txt",))
        self.assertEqual(escape.exception.code, "eval_v2_bundle_path_invalid")

        with self.assertRaises(EvalV2ContractError) as duplicate:
            sha256_bundle_v1(REPO_ROOT, ("pyproject.toml", "pyproject.toml"))
        self.assertEqual(duplicate.exception.code, "eval_v2_bundle_invalid")


if __name__ == "__main__":
    unittest.main()
