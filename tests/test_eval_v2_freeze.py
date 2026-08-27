from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

from researchops import eval_v2_freeze as freeze_module
from researchops.eval_v2_contracts import EvalV2ContractError
from researchops.eval_v2_freeze import (
    build_v8_diagnostic_candidate_component_hashes,
    candidate_commitment_sha256,
    load_public_regression_task_orders,
    sha256_bundle_v1,
    validate_eval_v2_dependency_environment,
    validate_public_regression_candidate,
)
from researchops.eval_v2_runner import (
    COMPLETION_FAILURE_SOURCES,
    COMPLETION_FAILURE_SOURCE_TO_ERROR_CODE,
)


REPO_ROOT = Path(__file__).resolve().parents[1]
CANDIDATE_PATH = REPO_ROOT / "evals" / "v2" / "public_regression_candidate_v7.json"
V8_CANDIDATE_PATH = (
    REPO_ROOT / "evals" / "v2" / "public_regression_candidate_v8.json"
)
V8_RUNTIME_PATH = (
    REPO_ROOT / "evals" / "v2" / "kimi_runtime_candidate_v8_contract.json"
)
HISTORICAL_V5_CANDIDATE_PATH = (
    REPO_ROOT / "evals" / "v2" / "public_regression_candidate_v5.json"
)
HISTORICAL_V1_CANDIDATE_PATH = (
    REPO_ROOT / "evals" / "v2" / "public_regression_candidate.json"
)
HISTORICAL_V2_CANDIDATE_PATH = (
    REPO_ROOT / "evals" / "v2" / "public_regression_candidate_v2.json"
)
HISTORICAL_V3_CANDIDATE_PATH = (
    REPO_ROOT / "evals" / "v2" / "public_regression_candidate_v3.json"
)
HISTORICAL_V4_CANDIDATE_PATH = (
    REPO_ROOT / "evals" / "v2" / "public_regression_candidate_v4.json"
)
HISTORICAL_V7_CANDIDATE_PATH = (
    REPO_ROOT / "evals" / "v2" / "public_regression_candidate_v7.json"
)
SPLIT_PATH = REPO_ROOT / "evals" / "v2" / "public_regression_split_manifest.json"


class EvalV2FreezeTests(unittest.TestCase):
    def test_repository_public_regression_candidate_is_locked_but_not_full_freeze(self) -> None:
        result = validate_public_regression_candidate(
            project_root=REPO_ROOT,
            candidate_path=CANDIDATE_PATH,
            verify_environment=False,
        )

        self.assertEqual(result["status"], "valid")
        self.assertEqual(result["candidate_status"], "candidate_locked")
        self.assertTrue(result["historical_snapshot_only"])
        self.assertFalse(result["full_campaign_frozen"])
        self.assertFalse(result["private_holdout_access_authorized"])
        self.assertFalse(result["model_quality_claim_allowed"])
        self.assertFalse(result["prior_results_inherited"])
        self.assertFalse(result["predecessor_failure_result_inherited"])
        self.assertFalse(result["predecessor_authorization_reused"])
        self.assertEqual(result["network_calls"], 0)
        self.assertEqual(
            result["predecessor_candidate_commitment_sha256"],
            "57d0c1b054367794fa08ec2dbdecdeb0c75bc4be2c45894b69db832a1f6f5641",
        )
        self.assertEqual(
            result["candidate_commitment_sha256"],
            "2d0b9952a556eed6982eac2b4e4d050efb40f518551738e51ceaf671a1d223d5",
        )

    def test_historical_candidate_cannot_claim_environment_verification(self) -> None:
        with self.assertRaises(EvalV2ContractError) as caught:
            validate_public_regression_candidate(
                project_root=REPO_ROOT,
                candidate_path=CANDIDATE_PATH,
                verify_environment=True,
            )
        self.assertEqual(
            caught.exception.code,
            "eval_v2_historical_candidate_environment_verification_unsupported",
        )

    def test_dependency_environment_gate_is_candidate_independent(self) -> None:
        result = validate_eval_v2_dependency_environment(REPO_ROOT)
        self.assertEqual(result["status"], "valid")
        self.assertEqual(
            result["verification_scope"],
            "requirements_lock_and_installed_environment_only",
        )
        self.assertFalse(result["candidate_verified"])
        self.assertFalse(result["historical_snapshot_verified"])
        self.assertTrue(result["dependency_lock"]["environment_verified"])
        self.assertEqual(result["dependency_lock"]["environment_mismatch_count"], 0)
        self.assertEqual(result["network_calls"], 0)

    def test_v8_diagnostic_candidate_is_current_but_not_online_authorized(self) -> None:
        result = validate_public_regression_candidate(
            project_root=REPO_ROOT,
            candidate_path=V8_CANDIDATE_PATH,
            verify_environment=True,
        )
        self.assertEqual(result["status"], "valid")
        self.assertEqual(
            result["candidate_id"],
            "eval-v2-public-regression-deepseek-kimi-controlled-chat-v8",
        )
        self.assertEqual(
            result["candidate_commitment_sha256"],
            "b41269ac6db96e2999fedc95f08f3b77a48699f8c0b50b63764bcb6e1f9e962c",
        )
        self.assertFalse(result["historical_snapshot_only"])
        self.assertTrue(result["diagnostic_snapshot_only"])
        self.assertFalse(result["public_regression_online_authorized"])
        self.assertFalse(result["controlled_synthetic_pilot_online_authorized"])
        self.assertFalse(result["model_quality_claim_allowed"])
        self.assertFalse(result["private_holdout_access_authorized"])
        self.assertFalse(result["prior_results_inherited"])
        self.assertFalse(result["predecessor_failure_result_inherited"])
        self.assertFalse(result["predecessor_authorization_reused"])
        self.assertTrue(result["dependency_lock"]["environment_verified"])
        self.assertEqual(result["dependency_lock"]["environment_mismatch_count"], 0)
        self.assertEqual(result["network_calls"], 0)
        self.assertEqual(
            hashlib.sha256(V8_CANDIDATE_PATH.read_bytes()).hexdigest(),
            "6615cc6638e79dc854265e46cf61dd4de9932e57a6fe2415b721f0a408b4eac0",
        )

    def test_v8_candidate_component_and_runtime_hashes_are_exact(self) -> None:
        candidate = json.loads(V8_CANDIDATE_PATH.read_text(encoding="utf-8"))
        self.assertEqual(
            candidate["component_hashes"],
            build_v8_diagnostic_candidate_component_hashes(REPO_ROOT),
        )
        self.assertEqual(
            candidate_commitment_sha256(candidate),
            candidate["candidate_commitment_sha256"],
        )
        runtime = json.loads(V8_RUNTIME_PATH.read_text(encoding="utf-8"))
        self.assertEqual(
            candidate["component_hashes"]["kimi_runtime_candidate_contract_sha256"],
            hashlib.sha256(V8_RUNTIME_PATH.read_bytes()).hexdigest(),
        )
        self.assertTrue(runtime["successor_safety_semantics"][
            "response_validation_diagnostic_fixed_enum_only"
        ])
        self.assertFalse(runtime["entry_points"]["public_runner_enabled"])
        self.assertFalse(
            runtime["entry_points"]["pilot_staging_provider_execution_enabled"]
        )
        self.assertFalse(
            runtime["entry_points"]["controlled_synthetic_pilot_online_authorized"]
        )
        self.assertTrue(all(value is False for value in runtime["claim_boundary"].values()))

    def test_v8_candidate_rejects_optimistic_pilot_and_runtime_mutations(self) -> None:
        real_loader = freeze_module._load_json_object
        for field, value in (
            ("campaign_registered", True),
            ("provider_registry_registered", True),
            ("synthetic_only", False),
            ("client_retries", 1),
            ("fallbacks_allowed", True),
        ):
            with self.subTest(field=field):
                def load_with_candidate_mutation(path: Path, label: str):
                    payload = real_loader(path, label)
                    if Path(path).name == V8_CANDIDATE_PATH.name:
                        payload = deepcopy(payload)
                        payload["controlled_synthetic_pilot_config"][field] = value
                    return payload

                with (
                    patch.object(
                        freeze_module,
                        "_load_json_object",
                        side_effect=load_with_candidate_mutation,
                    ),
                    self.assertRaises(EvalV2ContractError) as caught,
                ):
                    validate_public_regression_candidate(
                        project_root=REPO_ROOT,
                        candidate_path=V8_CANDIDATE_PATH,
                    )
                self.assertEqual(
                    caught.exception.code,
                    "eval_v2_public_candidate_v8_pilot_invalid",
                )

        for section, field, value in (
            ("entry_points", "public_runner_enabled", True),
            ("entry_points", "controlled_synthetic_pilot_online_authorized", True),
            ("audit_activity", "successor_online_calls_performed", 1),
            ("claim_boundary", "model_quality_claim_allowed", True),
            (
                "successor_safety_semantics",
                "response_validation_diagnostic_fixed_enum_only",
                False,
            ),
        ):
            with self.subTest(section=section, field=field):
                def load_with_mutation(path: Path, label: str):
                    payload = real_loader(path, label)
                    if Path(path).name == V8_RUNTIME_PATH.name:
                        payload = deepcopy(payload)
                        payload[section][field] = value
                    return payload

                with (
                    patch.object(
                        freeze_module,
                        "_load_json_object",
                        side_effect=load_with_mutation,
                    ),
                    self.assertRaises(EvalV2ContractError) as caught,
                ):
                    validate_public_regression_candidate(
                        project_root=REPO_ROOT,
                        candidate_path=V8_CANDIDATE_PATH,
                    )
                self.assertEqual(
                    caught.exception.code,
                    "eval_v2_kimi_runtime_candidate_v8_invalid",
                )

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

    def test_historical_v3_candidate_is_preserved_without_result_inheritance(self) -> None:
        raw = HISTORICAL_V3_CANDIDATE_PATH.read_bytes()
        historical = json.loads(raw)

        self.assertEqual(
            hashlib.sha256(raw).hexdigest(),
            "ba44823e9827c6c05e080dae69958ede27530002233f782b84dccc0944fcc3ee",
        )
        self.assertEqual(
            historical["candidate_commitment_sha256"],
            "22c985e9cf264df127be42756f708ff5c14e63fe00e5a0d3883efb781c50b2a9",
        )
        self.assertEqual(
            candidate_commitment_sha256(historical),
            historical["candidate_commitment_sha256"],
        )

    def test_historical_v4_candidate_is_preserved_without_result_inheritance(self) -> None:
        raw = HISTORICAL_V4_CANDIDATE_PATH.read_bytes()
        historical = json.loads(raw)

        self.assertEqual(
            hashlib.sha256(raw).hexdigest(),
            "22c635e210a1d699acd2cdff06d97683a5824d2405f24ee9698c990731346c9d",
        )
        self.assertEqual(
            historical["candidate_commitment_sha256"],
            "1741c2b0df53d06a299a5a89dfa91e68eade4c71cef7931d367115c07f6399c7",
        )
        self.assertEqual(
            candidate_commitment_sha256(historical),
            historical["candidate_commitment_sha256"],
        )

    def test_historical_v7_candidate_and_frozen_components_are_exact(self) -> None:
        raw = HISTORICAL_V7_CANDIDATE_PATH.read_bytes()
        historical = json.loads(raw)
        self.assertEqual(
            hashlib.sha256(raw).hexdigest(),
            "efc6ca2bbd97abe6c659983a386784938a74614cd1028861c25e20478ce7b278",
        )
        self.assertEqual(
            historical["candidate_commitment_sha256"],
            "2d0b9952a556eed6982eac2b4e4d050efb40f518551738e51ceaf671a1d223d5",
        )
        summary = validate_public_regression_candidate(
            project_root=REPO_ROOT,
            candidate_path=HISTORICAL_V7_CANDIDATE_PATH,
        )
        self.assertTrue(summary["historical_snapshot_only"])
        self.assertEqual(summary["historical_component_count"], 8)
        self.assertEqual(summary["network_calls"], 0)

        with tempfile.TemporaryDirectory() as temporary:
            isolated = Path(temporary)
            isolated_candidate = (
                isolated
                / "evals"
                / "v2"
                / "public_regression_candidate_v7.json"
            )
            isolated_candidate.parent.mkdir(parents=True)
            isolated_candidate.write_bytes(raw)
            with self.assertRaises(EvalV2ContractError) as caught:
                validate_public_regression_candidate(
                    project_root=isolated,
                    candidate_path=isolated_candidate,
                )
            self.assertEqual(
                caught.exception.code,
                "eval_v2_historical_candidate_v7_component_drift",
            )

    def test_current_public_candidate_remains_deepseek_only(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            payload = json.loads(
                HISTORICAL_V5_CANDIDATE_PATH.read_text(encoding="utf-8")
            )
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

    def test_kimi_provider_contract_cannot_optimistically_rewrite_unknowns(self) -> None:
        mutations = (
            (
                "privacy_and_retention_review",
                "input_output_or_feedback_model_optimization_license_present",
                False,
            ),
            (
                "privacy_and_retention_review",
                "request_body_retention_days",
                0,
            ),
            (
                "time_bounded_public_snapshot",
                "agreements_future_dated_at_review",
                False,
            ),
            (
                "time_bounded_public_snapshot",
                "snapshot_must_be_refreshed_before_chat_completions_or_pilot",
                False,
            ),
            (
                "time_bounded_public_snapshot",
                "account_rate_limits",
                {
                    "status": "verified",
                    "concurrency": 100,
                    "requests_per_minute": 500,
                    "tokens_per_minute": 300000,
                    "tokens_per_day": 1500000,
                },
            ),
            (
                "time_bounded_public_snapshot",
                "snapshot_is_spend_authorization",
                True,
            ),
            (
                "privacy_and_retention_review",
                "personal_information_allowed",
                True,
            ),
            (
                "privacy_and_retention_review",
                "file_upload_allowed",
                True,
            ),
            (
                "audit_activity",
                "models_preflight_requests_performed",
                1,
            ),
            (
                "metadata_preflight_design",
                "authorization_consumption_ledger_implemented",
                True,
            ),
            (
                "metadata_preflight_design",
                "success_authorizes_pilot",
                True,
            ),
            (
                "future_runtime_controls",
                "trust_environment_allowed",
                True,
            ),
            (
                "future_candidate_requirement",
                "minimum_chat_completions_successor_version",
                "v5",
            ),
        )
        real_loader = freeze_module._load_json_object

        for section, field, value in mutations:
            with self.subTest(section=section, field=field):
                def load_with_mutation(path: Path, label: str):
                    payload = real_loader(path, label)
                    if Path(path).name == "kimi_provider_contract.json":
                        payload = deepcopy(payload)
                        payload[section][field] = value
                    return payload

                with (
                    patch.object(
                        freeze_module,
                        "_load_json_object",
                        side_effect=load_with_mutation,
                    ),
                    self.assertRaises(EvalV2ContractError) as caught,
                ):
                    validate_public_regression_candidate(
                        project_root=REPO_ROOT,
                        candidate_path=HISTORICAL_V5_CANDIDATE_PATH,
                    )

                self.assertEqual(
                    caught.exception.code,
                    "eval_v2_kimi_provider_contract_invalid",
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
            payload = json.loads(
                HISTORICAL_V5_CANDIDATE_PATH.read_text(encoding="utf-8")
            )
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
