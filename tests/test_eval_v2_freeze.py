from __future__ import annotations

import hashlib
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
from researchops.eval_v2_runner import (
    COMPLETION_FAILURE_SOURCES,
    COMPLETION_FAILURE_SOURCE_TO_ERROR_CODE,
)


REPO_ROOT = Path(__file__).resolve().parents[1]
CANDIDATE_PATH = REPO_ROOT / "evals" / "v2" / "public_regression_candidate_v3.json"
HISTORICAL_V1_CANDIDATE_PATH = (
    REPO_ROOT / "evals" / "v2" / "public_regression_candidate.json"
)
HISTORICAL_V2_CANDIDATE_PATH = (
    REPO_ROOT / "evals" / "v2" / "public_regression_candidate_v2.json"
)
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
        self.assertFalse(result["prior_results_inherited"])
        self.assertEqual(
            result["completion_telemetry_contract_id"], "completion-telemetry-v2"
        )
        self.assertEqual(
            result["predecessor_candidate_commitment_sha256"],
            "1f6ac18e1cf4756e2a3ebd34075d2e98f8ab4dd98b316754f4af8b74c7be5ce5",
        )
        self.assertEqual(
            result["anthropic_provider_contract_id"],
            "eval-v2-anthropic-provider-offline-v1",
        )
        self.assertEqual(result["anthropic_provider_status"], "offline_contract_only")
        self.assertFalse(result["anthropic_campaign_registered"])
        self.assertFalse(result["anthropic_online_calls_performed"])
        self.assertEqual(result["provider_behavior_task_count"], 31)
        self.assertEqual(result["deterministic_fault_injection_task_count"], 9)
        self.assertEqual(result["dependency_lock"]["exact_pin_count"], 82)
        self.assertFalse(result["dependency_lock"]["artifact_hashes_included"])

    def test_historical_v1_candidate_is_preserved_without_result_inheritance(self) -> None:
        raw = HISTORICAL_V1_CANDIDATE_PATH.read_bytes()
        historical = json.loads(raw)

        self.assertEqual(
            hashlib.sha256(raw).hexdigest(),
            "b7ea7416c56b52e301c84aaa9c687b3925a64f11f6b5ae21f155ec27d67b8bfb",
        )
        self.assertEqual(
            historical["candidate_commitment_sha256"],
            "7744770aa4a36c131476b95d6ed9be248cdefc3ab0f4f2a18d5111b85c9f0d11",
        )
        self.assertEqual(
            candidate_commitment_sha256(historical),
            historical["candidate_commitment_sha256"],
        )

    def test_historical_v2_candidate_is_preserved_without_result_inheritance(self) -> None:
        raw = HISTORICAL_V2_CANDIDATE_PATH.read_bytes()
        historical = json.loads(raw)

        self.assertEqual(
            hashlib.sha256(raw).hexdigest(),
            "89b317f00a4d9a4f8f81ee59fb6d82e7ca225fd5d00fac450499ee2ce73b9a38",
        )
        self.assertEqual(
            historical["candidate_commitment_sha256"],
            "1f6ac18e1cf4756e2a3ebd34075d2e98f8ab4dd98b316754f4af8b74c7be5ce5",
        )
        self.assertEqual(
            candidate_commitment_sha256(historical),
            historical["candidate_commitment_sha256"],
        )

    def test_current_public_candidate_remains_deepseek_only(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            payload = json.loads(CANDIDATE_PATH.read_text(encoding="utf-8"))
            payload["provider_config"].update(
                {
                    "provider_id": "anthropic",
                    "model_id": "claude-sonnet-5",
                    "transport_id": "litellm_anthropic_chat_completions",
                }
            )
            payload["candidate_commitment_sha256"] = candidate_commitment_sha256(
                payload
            )
            path = Path(temporary) / "candidate.json"
            path.write_text(json.dumps(payload), encoding="utf-8")

            with self.assertRaises(EvalV2ContractError) as caught:
                validate_public_regression_candidate(
                    project_root=REPO_ROOT,
                    candidate_path=path,
                )

        self.assertEqual(
            caught.exception.code, "eval_v2_public_candidate_provider_invalid"
        )

    def test_completion_telemetry_machine_contract_matches_runtime_allowlist(self) -> None:
        contract = json.loads(
            (
                REPO_ROOT / "evals" / "v2" / "completion_telemetry_contract.json"
            ).read_text(encoding="utf-8")
        )
        sources = contract["failure_sources"]

        self.assertEqual(
            [item["source"] for item in sources], list(COMPLETION_FAILURE_SOURCES)
        )
        self.assertEqual(
            {item["source"]: item["error_code"] for item in sources},
            dict(COMPLETION_FAILURE_SOURCE_TO_ERROR_CODE),
        )
        self.assertEqual(contract["precedence"], list(COMPLETION_FAILURE_SOURCES))
        self.assertTrue(contract["diagnostic_only"])
        self.assertFalse(contract["causal_root_cause_claim_allowed"])
        self.assertIn("provider_response_body", contract["persistence_forbidden"])

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
