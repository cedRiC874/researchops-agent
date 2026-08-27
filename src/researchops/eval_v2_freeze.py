from __future__ import annotations

import hashlib
import importlib.metadata
import json
import random
import re
from pathlib import Path
from typing import Any, Mapping, Sequence

from .anthropic_preflight import anthropic_models_preflight_contract
from .eval_v2_contracts import EvalV2ContractError
from .eval_v2_provider_executor import eval_v2_prompt_contract
from .eval_v2_public import (
    load_eval_v2_dataset_manifest,
    load_eval_v2_public_tasks,
)
from .eval_v2_runner import eval_v2_tool_contract
from .kimi_preflight import kimi_models_preflight_contract


PUBLIC_REGRESSION_CANDIDATE_SCHEMA_VERSION = "5.0"
COMPLETION_TELEMETRY_CONTRACT_ID = "completion-telemetry-v2"
HISTORICAL_V1_CANDIDATE_COMMITMENT_SHA256 = (
    "7744770aa4a36c131476b95d6ed9be248cdefc3ab0f4f2a18d5111b85c9f0d11"
)
HISTORICAL_V2_CANDIDATE_COMMITMENT_SHA256 = (
    "1f6ac18e1cf4756e2a3ebd34075d2e98f8ab4dd98b316754f4af8b74c7be5ce5"
)
PREDECESSOR_V3_CANDIDATE_COMMITMENT_SHA256 = (
    "22c985e9cf264df127be42756f708ff5c14e63fe00e5a0d3883efb781c50b2a9"
)
PREDECESSOR_V4_CANDIDATE_COMMITMENT_SHA256 = (
    "1741c2b0df53d06a299a5a89dfa91e68eade4c71cef7931d367115c07f6399c7"
)
HISTORICAL_V6_CANDIDATE_COMMITMENT_SHA256 = (
    "57d0c1b054367794fa08ec2dbdecdeb0c75bc4be2c45894b69db832a1f6f5641"
)
HISTORICAL_V7_CANDIDATE_COMMITMENT_SHA256 = (
    "2d0b9952a556eed6982eac2b4e4d050efb40f518551738e51ceaf671a1d223d5"
)
_SHA256 = re.compile(r"^[a-f0-9]{64}$")
_EXACT_REQUIREMENT = re.compile(r"^(?P<name>[A-Za-z0-9_.-]+)==(?P<version>[^\s]+)$")
_FAULT_SCENARIOS = frozenset(
    {"provider_timeout", "output_truncation", "side_effect_outcome_unknown"}
)
_CANDIDATE_FIELDS = frozenset(
    {
        "schema_version",
        "candidate_id",
        "status",
        "locked_at_utc",
        "scope",
        "campaign_id",
        "campaign_status_expected",
        "full_campaign_frozen",
        "private_holdout_access_authorized",
        "model_quality_claim_allowed",
        "predecessor_candidate_commitment_sha256",
        "prior_results_inherited",
        "completion_telemetry_contract_id",
        "anthropic_models_preflight_contract_id",
        "kimi_provider_contract_id",
        "kimi_models_preflight_contract_id",
        "execution_policy",
        "provider_config",
        "hash_algorithm",
        "component_hashes",
        "candidate_commitment_sha256",
        "limitations",
    }
)
_HISTORICAL_V7_CANDIDATE_FIELDS = frozenset(
    {
        "schema_version",
        "candidate_id",
        "status",
        "locked_at_utc",
        "scope",
        "campaign_id",
        "campaign_status_expected",
        "full_campaign_frozen",
        "private_holdout_access_authorized",
        "model_quality_claim_allowed",
        "predecessor_candidate_commitment_sha256",
        "prior_results_inherited",
        "predecessor_failure_result_inherited",
        "predecessor_authorization_reused",
        "completion_telemetry_contract_id",
        "anthropic_models_preflight_contract_id",
        "kimi_provider_contract_id",
        "kimi_models_preflight_contract_id",
        "kimi_terms_g2a_contract_id",
        "kimi_chat_completions_contract_id",
        "kimi_controlled_pilot_contract_id",
        "kimi_controlled_pilot_scenario_set_id",
        "kimi_runtime_candidate_contract_id",
        "execution_policy",
        "provider_config",
        "controlled_synthetic_pilot_config",
        "hash_algorithm",
        "component_hashes",
        "candidate_commitment_sha256",
        "limitations",
    }
)
_HISTORICAL_V7_LIMITATIONS = (
    "This locks a versioned offline successor candidate only; the public-regression execution Provider remains DeepSeek and the full Eval v2 campaign remains design_only.",
    "Candidate v6, its consumed authorization and its post-lock usage-stage failure are immutable predecessor evidence and are not inherited as a v7 result, authorization, compatibility claim or causal root-cause claim.",
    "The live v1 response shape was not persisted and remains unknown; v2 accepts only the first-party-documented terminal event with one non-empty choice, finish_reason and one reconciled top-level, choice-level or dual usage projection before one DONE marker.",
    "The v2 parser treats cached_tokens as optional, exposes an omitted value as null and budgets all prompt tokens at the uncached rate; cache information cannot relax the local reservation.",
    "The Kimi G2a terms decision is provisional synthetic-only and expires at 2026-08-30T16:00:00Z; it is not final effective-terms verification and does not itself authorize Provider calls.",
    "The v2 Kimi Chat Completions transport and v2 controlled pilot are implemented and MockTransport-tested but have performed zero successor online calls.",
    "Kimi remains absent from the generic Provider registry, Phase 6, self-pilot, Web, public-runner, pilot-staging Provider execution and private-evaluation entrypoints.",
    "The controlled pilot reuses exactly the immutable v1 three-case synthetic scenario set and v1 synthetic tool schema; no prior public, supervised, repo-local holdout or private item was used for prompt, scorer, schema, candidate or task-selection tuning.",
    "A future online successor pilot requires a fresh Key, exact locked caps, fresh bound legal and Kimi K3 pricing attestations, a CNY 5 local conservative hard-stop and a new separately authorized single-use capability bound to this candidate commitment; no retry, resume, fallback or legacy authorization reuse is allowed.",
    "Actual Provider token use, billing, latency, Chat/tool/error compatibility and model quality remain unverified for v2; all receipt authorization and quality claims remain false.",
    "The announced 2026-08-31 terms remain a pre-effective preview and require final source capture, hashing, delta review and manual signoff at or after effectiveness.",
    "Provider-behavior and deterministic fault-injection channels remain separate, completion failures are diagnostic rather than causal root-cause claims, and requirements.lock still lacks artifact hashes.",
    "No online Provider call or paid evaluation was performed while building Candidate v7.",
)
_EXECUTION_POLICY_FIELDS = frozenset(
    {
        "repetitions_per_provider",
        "randomized_case_order",
        "precommitted_orders",
        "split_manifest_id",
        "provider_behavior_task_count",
        "deterministic_fault_injection_task_count",
        "fault_results_attributed_to_model",
        "execution_channels_reported_separately",
    }
)
_PROVIDER_CONFIG_FIELDS = frozenset(
    {
        "provider_id",
        "model_id",
        "transport_id",
        "max_turns",
        "case_timeout_seconds",
        "normal_max_output_tokens",
        "refusal_max_output_tokens",
        "clarification_max_output_tokens",
    }
)
_COMPONENT_KEYS = frozenset(
    {
        "source_bundle_sha256",
        "prompt_contract_sha256",
        "prompt_source_sha256",
        "scorer_bundle_sha256",
        "tool_contract_sha256",
        "tool_source_bundle_sha256",
        "split_manifest_sha256",
        "public_tasks_sha256",
        "public_task_schema_sha256",
        "dataset_manifest_sha256",
        "internal_review_sha256",
        "dependency_bundle_sha256",
        "pyproject_sha256",
        "requirements_lock_sha256",
        "completion_telemetry_contract_sha256",
        "anthropic_provider_contract_sha256",
        "anthropic_models_preflight_contract_sha256",
        "kimi_provider_contract_sha256",
        "kimi_models_preflight_contract_sha256",
        "campaign_sha256",
    }
)
_KIMI_ACCOUNT_RATE_LIMITS = {
    "status": "account_specific_and_under_announced_update",
    "concurrency": None,
    "requests_per_minute": None,
    "tokens_per_minute": None,
    "tokens_per_day": None,
}
_KIMI_FUTURE_RUNTIME_CONTROLS = {
    "fixed_origin_required": True,
    "trust_environment_allowed": False,
    "redirects_allowed": False,
    "client_managed_retries": 0,
    "fallbacks_allowed": False,
    "external_tracing_or_callbacks_allowed": False,
    "raw_request_or_response_body_logging_allowed": False,
    "api_key_persisted_logged_or_hashed_allowed": False,
    "api_key_loaded_after_local_gates": True,
    "owned_http_client_per_run_required": True,
    "local_request_count_cap_required": True,
    "local_input_token_cap_required": True,
    "local_output_token_cap_required": True,
    "local_monetary_cap_required": True,
    "provider_project_budget_enforcement_delay_minutes_approximate": 10,
    "provider_project_budget_is_local_hard_cap": False,
}
_KIMI_METADATA_PREFLIGHT_DESIGN = {
    "implementation_status": "implemented_offline_tested_not_run",
    "method": "GET",
    "fixed_origin": "https://api.moonshot.cn",
    "path": "/v1/models",
    "future_http_attempts_max": 1,
    "future_client_retries": 0,
    "model_generation_calls": 0,
    "model_token_calls": 0,
    "live_preflight_performed": False,
    "success_claim_scope": (
        "china_platform_authentication_and_exact_model_visibility_"
        "at_one_point_in_time_only"
    ),
    "success_authorizes_chat_completions": False,
    "success_authorizes_tools": False,
    "success_authorizes_pilot": False,
    "success_authorizes_campaign_registration": False,
    "success_authorizes_private_evaluation": False,
    "success_allows_model_quality_claim": False,
    "separate_one_time_authorization_required": True,
    "authorization_consumption_ledger_implemented": False,
}


def build_public_regression_component_hashes(
    project_root: str | Path,
) -> dict[str, str]:
    root = Path(project_root).resolve()
    source_files = sorted(
        path.relative_to(root).as_posix()
        for path in (root / "src" / "researchops").rglob("*.py")
    )
    return {
        "source_bundle_sha256": sha256_bundle_v1(root, source_files),
        "prompt_contract_sha256": _sha256_file(
            root / "evals" / "v2" / "provider_prompt_contract.json"
        ),
        "prompt_source_sha256": _sha256_file(
            root / "src" / "researchops" / "eval_v2_provider_executor.py"
        ),
        "scorer_bundle_sha256": sha256_bundle_v1(
            root,
            (
                "src/researchops/eval_v2_contracts.py",
                "src/researchops/eval_v2_public.py",
                "src/researchops/eval_v2_runner.py",
            ),
        ),
        "tool_contract_sha256": _sha256_file(
            root / "evals" / "v2" / "tool_contract.json"
        ),
        "tool_source_bundle_sha256": sha256_bundle_v1(
            root,
            (
                "src/researchops/eval_v2_provider_executor.py",
                "src/researchops/eval_v2_runner.py",
            ),
        ),
        "split_manifest_sha256": _sha256_file(
            root / "evals" / "v2" / "public_regression_split_manifest.json"
        ),
        "public_tasks_sha256": _sha256_file(
            root / "evals" / "v2" / "public_tasks.jsonl"
        ),
        "public_task_schema_sha256": _sha256_file(
            root / "evals" / "v2" / "public_task_schema.json"
        ),
        "dataset_manifest_sha256": _sha256_file(
            root / "evals" / "v2" / "external_datasets.json"
        ),
        "internal_review_sha256": _sha256_file(
            root / "evals" / "v2" / "internal_review.json"
        ),
        "dependency_bundle_sha256": sha256_bundle_v1(
            root, ("pyproject.toml", "requirements.lock")
        ),
        "pyproject_sha256": _sha256_file(root / "pyproject.toml"),
        "requirements_lock_sha256": _sha256_file(root / "requirements.lock"),
        "completion_telemetry_contract_sha256": _sha256_file(
            root / "evals" / "v2" / "completion_telemetry_contract.json"
        ),
        "anthropic_provider_contract_sha256": _sha256_file(
            root / "evals" / "v2" / "anthropic_provider_contract.json"
        ),
        "anthropic_models_preflight_contract_sha256": _sha256_file(
            root / "evals" / "v2" / "anthropic_models_preflight_contract.json"
        ),
        "kimi_provider_contract_sha256": _sha256_file(
            root / "evals" / "v2" / "kimi_provider_contract.json"
        ),
        "kimi_models_preflight_contract_sha256": _sha256_file(
            root / "evals" / "v2" / "kimi_models_preflight_contract.json"
        ),
        "campaign_sha256": _sha256_file(root / "evals" / "v2" / "campaign.json"),
    }


def _validate_historical_v7_candidate(
    *,
    root: Path,
    candidate_path: Path,
) -> dict[str, Any]:
    expected_path = (
        root / "evals" / "v2" / "public_regression_candidate_v7.json"
    ).resolve()
    if candidate_path.resolve() != expected_path:
        raise EvalV2ContractError(
            "eval_v2_public_candidate_path_invalid",
            "Historical Candidate v7 必须使用固定 manifest 路径。",
        )

    candidate = _load_json_object(candidate_path, "historical candidate v7")
    _require_exact_fields(
        candidate,
        _HISTORICAL_V7_CANDIDATE_FIELDS,
        "historical candidate v7",
    )
    if (
        _sha256_file(candidate_path)
        != "efc6ca2bbd97abe6c659983a386784938a74614cd1028861c25e20478ce7b278"
        or candidate.get("schema_version") != "7.0"
        or candidate.get("candidate_id")
        != "eval-v2-public-regression-deepseek-kimi-controlled-chat-v7"
        or candidate.get("status") != "candidate_locked"
        or candidate.get("locked_at_utc") != "2026-08-26T17:55:43Z"
        or candidate.get("scope") != "public_regression"
        or candidate.get("campaign_status_expected") != "design_only"
        or candidate.get("full_campaign_frozen") is not False
        or candidate.get("private_holdout_access_authorized") is not False
        or candidate.get("model_quality_claim_allowed") is not False
        or candidate.get("predecessor_candidate_commitment_sha256")
        != HISTORICAL_V6_CANDIDATE_COMMITMENT_SHA256
        or candidate.get("prior_results_inherited") is not False
        or candidate.get("predecessor_failure_result_inherited") is not False
        or candidate.get("predecessor_authorization_reused") is not False
        or candidate.get("limitations") != list(_HISTORICAL_V7_LIMITATIONS)
        or candidate.get("candidate_commitment_sha256")
        != HISTORICAL_V7_CANDIDATE_COMMITMENT_SHA256
        or candidate_commitment_sha256(candidate)
        != HISTORICAL_V7_CANDIDATE_COMMITMENT_SHA256
    ):
        raise EvalV2ContractError(
            "eval_v2_historical_candidate_v7_invalid",
            "Historical Candidate v7 manifest 与冻结快照不一致。",
        )

    frozen_components = {
        "evals/v2/kimi_runtime_candidate_v7_contract.json": (
            "12ad1f34398cc4579a2b43888057fc80eca31c0174cd7e78a09d46486b579eb9"
        ),
        "src/researchops/kimi_chat_transport_v2.py": (
            "0e62e6b696a43804ef36bd1b7c1422cb0b9d7a974544d2afe50f5b7c6e2af8ae"
        ),
        "evals/v2/kimi_chat_completions_contract_v2.json": (
            "eb226578df2555813fbef005e366bb014d05bbb0dfde039b170689fc5a00916c"
        ),
        "src/researchops/kimi_controlled_pilot_v2.py": (
            "6dc1ea634cb944c35bb072227d1f698db35a6e7db7a2bbe6f9e83f547f168ee1"
        ),
        "evals/v2/kimi_controlled_pilot_contract_v2.json": (
            "6c305f7bf53ec2b10dca16a4fdbec157d3cbfcb8a6804e58641c5c0d848ff605"
        ),
        "evals/v2/kimi_controlled_pilot_scenarios_v1.json": (
            "f5f51f57f46d5b98677bb9809f28f3e211bb553fd8f4f8d873f848a9ef6f649b"
        ),
        "src/researchops/kimi_synthetic_tools.py": (
            "e097d5371b36afc5dc7bf5a87cd206d62092a262db4feed35d2a0f4d6b5fc130"
        ),
        "evals/v2/kimi_terms_g2a_provisional_contract.json": (
            "be9b3b609675b6965d81d078c6c260b75bad4b99f98276b811ac533deadfdc49"
        ),
    }
    if any(
        (root / relative_path).is_symlink()
        or not (root / relative_path).is_file()
        or _sha256_file(root / relative_path) != expected_sha256
        for relative_path, expected_sha256 in frozen_components.items()
    ):
        raise EvalV2ContractError(
            "eval_v2_historical_candidate_v7_component_drift",
            "Historical Candidate v7 frozen components 缺失或漂移。",
        )

    return {
        "status": "valid",
        "candidate_status": "candidate_locked",
        "candidate_id": candidate["candidate_id"],
        "candidate_commitment_sha256": (
            HISTORICAL_V7_CANDIDATE_COMMITMENT_SHA256
        ),
        "full_campaign_frozen": False,
        "private_holdout_access_authorized": False,
        "model_quality_claim_allowed": False,
        "predecessor_candidate_commitment_sha256": (
            HISTORICAL_V6_CANDIDATE_COMMITMENT_SHA256
        ),
        "prior_results_inherited": False,
        "predecessor_failure_result_inherited": False,
        "predecessor_authorization_reused": False,
        "historical_snapshot_only": True,
        "historical_component_count": len(frozen_components),
        "network_calls": 0,
    }


def validate_public_regression_candidate(
    *,
    project_root: str | Path,
    candidate_path: str | Path,
    verify_environment: bool = False,
) -> dict[str, Any]:
    root = Path(project_root).resolve()
    resolved_candidate_path = Path(candidate_path).resolve()
    if resolved_candidate_path.name == "public_regression_candidate_v7.json":
        if verify_environment:
            raise EvalV2ContractError(
                "eval_v2_historical_candidate_environment_verification_unsupported",
                "Historical Candidate v7 只能复核冻结 artifact；不能据此声明当前环境已验证。",
            )
        return _validate_historical_v7_candidate(
            root=root,
            candidate_path=resolved_candidate_path,
        )
    candidate = _load_json_object(resolved_candidate_path, "candidate")
    _require_exact_fields(candidate, _CANDIDATE_FIELDS, "candidate")
    if (
        candidate["schema_version"] != PUBLIC_REGRESSION_CANDIDATE_SCHEMA_VERSION
        or candidate["status"] != "candidate_locked"
        or candidate["scope"] != "public_regression"
        or candidate["campaign_status_expected"] != "design_only"
        or candidate["full_campaign_frozen"] is not False
        or candidate["private_holdout_access_authorized"] is not False
        or candidate["model_quality_claim_allowed"] is not False
        or candidate["predecessor_candidate_commitment_sha256"]
        != PREDECESSOR_V4_CANDIDATE_COMMITMENT_SHA256
        or candidate["prior_results_inherited"] is not False
        or candidate["completion_telemetry_contract_id"]
        != COMPLETION_TELEMETRY_CONTRACT_ID
        or candidate["anthropic_models_preflight_contract_id"]
        != "eval-v2-anthropic-models-preflight-v1"
        or candidate["kimi_provider_contract_id"]
        != "eval-v2-moonshot-kimi-provider-design-v1"
        or candidate["kimi_models_preflight_contract_id"]
        != "eval-v2-kimi-models-preflight-v1"
        or candidate["hash_algorithm"] != "sha256-bundle-v1"
    ):
        raise EvalV2ContractError(
            "eval_v2_public_candidate_invalid",
            "Public-regression candidate 状态或安全边界无效。",
        )

    campaign = _load_json_object(root / "evals" / "v2" / "campaign.json", "campaign")
    if (
        campaign.get("campaign_id") != candidate["campaign_id"]
        or campaign.get("status") != "design_only"
    ):
        raise EvalV2ContractError(
            "eval_v2_public_candidate_campaign_mismatch",
            "Candidate 与 design-only campaign 不匹配。",
        )
    run_policy = campaign.get("run_policy")
    provider_slots = (
        run_policy.get("providers") if isinstance(run_policy, Mapping) else None
    )
    if provider_slots != [
        {
            "provider_id": "deepseek",
            "status": "registered",
            "model_id": "deepseek-v4-flash",
            "transport_id": "openai_compatible_responses",
        },
        {
            "provider_id": "second_provider",
            "status": "planned",
            "model_id": None,
            "transport_id": None,
        },
    ]:
        raise EvalV2ContractError(
            "eval_v2_public_candidate_provider_plan_invalid",
            "Offline Provider/preflight contract 不得冒充已注册 campaign Provider。",
        )

    execution = _strict_object(
        candidate["execution_policy"], _EXECUTION_POLICY_FIELDS, "execution_policy"
    )
    provider = _strict_object(
        candidate["provider_config"], _PROVIDER_CONFIG_FIELDS, "provider_config"
    )
    if (
        execution["repetitions_per_provider"] != 3
        or execution["randomized_case_order"] is not True
        or execution["precommitted_orders"] is not True
        or execution["provider_behavior_task_count"] != 31
        or execution["deterministic_fault_injection_task_count"] != 9
        or execution["fault_results_attributed_to_model"] is not False
        or execution["execution_channels_reported_separately"] is not True
    ):
        raise EvalV2ContractError(
            "eval_v2_public_candidate_execution_invalid",
            "Public-regression execution channel/ordering policy 无效。",
        )
    if provider != {
        "provider_id": "deepseek",
        "model_id": "deepseek-v4-flash",
        "transport_id": "openai_compatible_responses",
        "max_turns": 8,
        "case_timeout_seconds": 120,
        "normal_max_output_tokens": 2000,
        "refusal_max_output_tokens": 512,
        "clarification_max_output_tokens": 768,
    }:
        raise EvalV2ContractError(
            "eval_v2_public_candidate_provider_invalid",
            "Candidate provider/runtime 配置不是预承诺值。",
        )

    prompt_snapshot = _load_json_object(
        root / "evals" / "v2" / "provider_prompt_contract.json", "prompt contract"
    )
    tool_snapshot = _load_json_object(
        root / "evals" / "v2" / "tool_contract.json", "tool contract"
    )
    completion_snapshot = _load_json_object(
        root / "evals" / "v2" / "completion_telemetry_contract.json",
        "completion telemetry contract",
    )
    anthropic_snapshot = _load_json_object(
        root / "evals" / "v2" / "anthropic_provider_contract.json",
        "Anthropic provider contract",
    )
    anthropic_preflight_snapshot = _load_json_object(
        root / "evals" / "v2" / "anthropic_models_preflight_contract.json",
        "Anthropic Models preflight contract",
    )
    kimi_snapshot = _load_json_object(
        root / "evals" / "v2" / "kimi_provider_contract.json",
        "Kimi provider contract",
    )
    kimi_preflight_snapshot = _load_json_object(
        root / "evals" / "v2" / "kimi_models_preflight_contract.json",
        "Kimi Models preflight contract",
    )
    if prompt_snapshot != eval_v2_prompt_contract():
        raise EvalV2ContractError(
            "eval_v2_public_candidate_prompt_drift",
            "Provider prompt contract snapshot 与源码不一致。",
        )
    if tool_snapshot != eval_v2_tool_contract():
        raise EvalV2ContractError(
            "eval_v2_public_candidate_tool_drift",
            "Tool contract snapshot 与源码不一致。",
        )
    if (
        completion_snapshot.get("schema_version") != "2.0"
        or completion_snapshot.get("contract_id")
        != COMPLETION_TELEMETRY_CONTRACT_ID
        or completion_snapshot.get("diagnostic_only") is not True
        or completion_snapshot.get("causal_root_cause_claim_allowed") is not False
    ):
        raise EvalV2ContractError(
            "eval_v2_public_candidate_completion_contract_invalid",
            "Completion telemetry contract 状态或声明边界无效。",
        )
    if anthropic_snapshot != _expected_anthropic_provider_contract():
        raise EvalV2ContractError(
            "eval_v2_anthropic_provider_contract_invalid",
            "Anthropic offline Provider contract 状态或声明边界无效。",
        )
    if anthropic_preflight_snapshot != anthropic_models_preflight_contract():
        raise EvalV2ContractError(
            "eval_v2_anthropic_models_preflight_contract_invalid",
            "Anthropic Models preflight contract 与实现不一致。",
        )
    if (
        kimi_snapshot.get("schema_version") != "1.0"
        or kimi_snapshot.get("contract_id")
        != "eval-v2-moonshot-kimi-provider-design-v1"
        or kimi_snapshot.get("status")
        != "preflight_implemented_offline_tested_not_run"
        or kimi_snapshot.get("implementation_status") != "models_preflight_only"
        or kimi_snapshot.get("provider")
        != {
            "provider_id": "moonshot_kimi",
            "provider_class": "first_party_model_api",
            "platform_surface": "platform.kimi.com",
            "region_surface": "china",
            "default_model_id": "kimi-k3",
            "allowed_model_ids": ["kimi-k3"],
            "transport_id": "moonshot_openai_compatible_chat_completions",
            "transport_protocol": "chat_completions",
            "api_origin": "https://api.moonshot.cn",
            "api_base": "https://api.moonshot.cn/v1",
            "chat_completions_path": "/v1/chat/completions",
            "models_path": "/v1/models",
            "api_key_environment_variable": "MOONSHOT_API_KEY",
            "authentication_scheme": "bearer",
            "openai_protocol_compatibility_is_provider_identity": False,
            "arbitrary_base_url_allowed": False,
            "arbitrary_model_id_allowed": False,
        }
        or kimi_snapshot.get("privacy_and_retention_review", {}).get(
            "synthetic_data_only"
        )
        is not True
        or kimi_snapshot.get("privacy_and_retention_review", {}).get(
            "input_output_or_feedback_model_optimization_license_present"
        )
        is not True
        or kimi_snapshot.get("privacy_and_retention_review", {}).get(
            "public_no_training_commitment_verified"
        )
        is not False
        or kimi_snapshot.get("privacy_and_retention_review", {}).get(
            "public_training_opt_out_verified"
        )
        is not False
        or any(
            kimi_snapshot.get("privacy_and_retention_review", {}).get(key)
            is not None
            for key in (
                "request_body_retention_days",
                "response_body_retention_days",
                "tool_input_and_output_retention_days",
                "request_metadata_retention_days",
                "abuse_and_security_log_retention_days",
                "backup_retention_days",
                "deletion_sla_days",
            )
        )
        or kimi_snapshot.get("privacy_and_retention_review", {}).get(
            "zero_data_retention_verified"
        )
        is not False
        or kimi_snapshot.get("privacy_and_retention_review", {}).get(
            "non_synthetic_data_allowed"
        )
        is not False
        or any(
            kimi_snapshot.get("privacy_and_retention_review", {}).get(key)
            is not False
            for key in (
                "complete_api_subprocessor_list_verified",
                "public_dpa_verified",
                "personal_information_allowed",
                "business_secrets_allowed",
                "file_upload_allowed",
                "hosted_web_search_allowed",
                "hosted_memory_allowed",
            )
        )
        or kimi_snapshot.get("privacy_and_retention_review", {}).get(
            "private_holdout_allowed"
        )
        is not False
        or kimi_snapshot.get("time_bounded_public_snapshot", {}).get(
            "agreements_future_dated_at_review"
        )
        is not True
        or kimi_snapshot.get("time_bounded_public_snapshot", {}).get(
            "published_agreements_effective_on"
        )
        != "2026-08-31"
        or kimi_snapshot.get("time_bounded_public_snapshot", {}).get(
            "rereview_not_before"
        )
        != "2026-08-31"
        or kimi_snapshot.get("time_bounded_public_snapshot", {}).get(
            "snapshot_must_be_refreshed_before_chat_completions_or_pilot"
        )
        is not True
        or kimi_snapshot.get("time_bounded_public_snapshot", {}).get(
            "metadata_preflight_allowed_under_current_effective_terms"
        )
        is not True
        or kimi_snapshot.get("time_bounded_public_snapshot", {}).get(
            "account_rate_limits"
        )
        != _KIMI_ACCOUNT_RATE_LIMITS
        or kimi_snapshot.get("time_bounded_public_snapshot", {}).get(
            "snapshot_is_spend_authorization"
        )
        is not False
        or kimi_snapshot.get("future_runtime_controls")
        != _KIMI_FUTURE_RUNTIME_CONTROLS
        or kimi_snapshot.get("metadata_preflight_design")
        != _KIMI_METADATA_PREFLIGHT_DESIGN
        or any(
            kimi_snapshot.get("audit_activity", {}).get(key) != 0
            for key in (
                "authenticated_moonshot_api_requests_performed",
                "models_preflight_requests_performed",
                "chat_completions_requests_performed",
                "tool_calls_performed",
                "model_token_calls",
            )
        )
        or kimi_snapshot.get("audit_activity", {}).get(
            "api_key_read_or_used_by_this_design"
        )
        is not False
        or kimi_snapshot.get("candidate_binding_status")
        != "bound_to_candidate_v5"
        or kimi_snapshot.get("future_candidate_requirement", {}).get(
            "minimum_chat_completions_successor_version"
        )
        != "v6"
        or kimi_snapshot.get("future_candidate_requirement", {}).get(
            "campaign_modified"
        )
        is not False
        or kimi_snapshot.get("future_candidate_requirement", {}).get(
            "historical_evidence_modified"
        )
        is not False
        or kimi_snapshot.get("entry_points", {}).get("metadata_preflight_enabled")
        is not True
        or any(
            kimi_snapshot.get("entry_points", {}).get(key) is not False
            for key in (
                "provider_registry_enabled",
                "phase6_cli_enabled",
                "self_pilot_cli_enabled",
                "self_pilot_web_enabled",
                "eval_v2_public_runner_enabled",
                "pilot_staging_enabled",
                "controlled_synthetic_pilot_enabled",
                "private_evaluation_enabled",
            )
        )
        or kimi_snapshot.get("evaluation_boundary", {}).get("campaign_registered")
        is not False
        or kimi_snapshot.get("evaluation_boundary", {}).get(
            "online_calls_performed"
        )
        is not False
        or kimi_snapshot.get("evaluation_boundary", {}).get(
            "model_quality_claim_allowed"
        )
        is not False
    ):
        raise EvalV2ContractError(
            "eval_v2_kimi_provider_contract_invalid",
            "Kimi provider contract 状态或声明边界无效。",
        )
    if kimi_preflight_snapshot != kimi_models_preflight_contract():
        raise EvalV2ContractError(
            "eval_v2_kimi_models_preflight_contract_invalid",
            "Kimi Models preflight contract 与实现不一致。",
        )

    split = _validate_public_regression_split(root)
    if split["manifest_id"] != execution["split_manifest_id"]:
        raise EvalV2ContractError(
            "eval_v2_public_candidate_split_mismatch",
            "Candidate split manifest ID 不匹配。",
        )

    expected_hashes = _strict_object(
        candidate["component_hashes"], _COMPONENT_KEYS, "component_hashes"
    )
    if not all(isinstance(value, str) and _SHA256.fullmatch(value) for value in expected_hashes.values()):
        raise EvalV2ContractError(
            "eval_v2_public_candidate_hash_invalid",
            "Candidate component hash 必须是小写 SHA-256。",
        )
    actual_hashes = build_public_regression_component_hashes(root)
    if dict(expected_hashes) != actual_hashes:
        changed = sorted(
            key for key in _COMPONENT_KEYS if expected_hashes.get(key) != actual_hashes[key]
        )
        raise EvalV2ContractError(
            "eval_v2_public_candidate_component_drift",
            "Public-regression candidate component drift：" + ", ".join(changed),
        )

    commitment = candidate.get("candidate_commitment_sha256")
    if not isinstance(commitment, str) or _SHA256.fullmatch(commitment) is None:
        raise EvalV2ContractError(
            "eval_v2_public_candidate_commitment_invalid",
            "Candidate commitment 必须是 SHA-256。",
        )
    if commitment != candidate_commitment_sha256(candidate):
        raise EvalV2ContractError(
            "eval_v2_public_candidate_commitment_mismatch",
            "Candidate commitment 与 manifest 内容不匹配。",
        )

    lock_summary = _validate_requirements_lock(
        root / "requirements.lock", verify_environment=verify_environment
    )
    return {
        "status": "valid",
        "candidate_status": "candidate_locked",
        "candidate_id": candidate["candidate_id"],
        "candidate_commitment_sha256": commitment,
        "full_campaign_frozen": False,
        "private_holdout_access_authorized": False,
        "model_quality_claim_allowed": False,
        "predecessor_candidate_commitment_sha256": (
            PREDECESSOR_V4_CANDIDATE_COMMITMENT_SHA256
        ),
        "prior_results_inherited": False,
        "historical_snapshot_only": False,
        "completion_telemetry_contract_id": COMPLETION_TELEMETRY_CONTRACT_ID,
        "anthropic_provider_contract_id": anthropic_snapshot["contract_id"],
        "anthropic_provider_status": "offline_contract_only",
        "anthropic_models_preflight_contract_id": (
            anthropic_preflight_snapshot["contract_id"]
        ),
        "anthropic_models_preflight_status": (
            anthropic_preflight_snapshot["implementation_status"]
        ),
        "anthropic_models_preflight_live_calls_performed": False,
        "anthropic_campaign_registered": False,
        "anthropic_online_calls_performed": False,
        "kimi_provider_contract_id": kimi_snapshot["contract_id"],
        "kimi_provider_status": kimi_snapshot["status"],
        "kimi_models_preflight_contract_id": kimi_preflight_snapshot[
            "contract_id"
        ],
        "kimi_models_preflight_status": kimi_preflight_snapshot[
            "implementation_status"
        ],
        "kimi_models_preflight_live_calls_performed": False,
        "kimi_campaign_registered": False,
        "kimi_online_calls_performed": False,
        "public_regression_task_count": 40,
        "provider_behavior_task_count": 31,
        "deterministic_fault_injection_task_count": 9,
        "repetitions": 3,
        "task_order_sha256": split["task_order_sha256"],
        "dependency_lock": lock_summary,
        "network_calls": 0,
    }


def validate_eval_v2_dependency_environment(
    project_root: str | Path,
) -> dict[str, Any]:
    """Validate the installed environment without treating a historical snapshot as current."""

    root = Path(project_root).resolve()
    dependency_lock = _validate_requirements_lock(
        root / "requirements.lock", verify_environment=True
    )
    return {
        "status": "valid",
        "verification_scope": "requirements_lock_and_installed_environment_only",
        "candidate_verified": False,
        "historical_snapshot_verified": False,
        "dependency_lock": dependency_lock,
        "network_calls": 0,
    }


def _expected_anthropic_provider_contract() -> dict[str, Any]:
    return {
        "schema_version": "1.0",
        "contract_id": "eval-v2-anthropic-provider-offline-v1",
        "implementation_status": "offline_contract_only",
        "provider": {
            "provider_id": "anthropic",
            "default_model_id": "claude-sonnet-5",
            "allowed_model_ids": [
                "claude-sonnet-5",
                "claude-opus-4-8",
                "claude-haiku-4-5-20251001",
            ],
            "transport_id": "litellm_anthropic_chat_completions",
            "api_base": "https://api.anthropic.com",
            "api_key_environment_variable": "ANTHROPIC_API_KEY",
        },
        "dependency_contract": {
            "openai_agents_version": "0.21.0",
            "litellm_version": "1.83.0",
            "compatibility_pin_exact": True,
            "artifact_hashes_in_lock": False,
        },
        "execution_controls": {
            "owned_http_client_per_run": True,
            "single_process_run_policy": (
                "fail_closed_on_concurrent_anthropic_run"
            ),
            "dependency_pin_checked_at_execution": True,
            "request_timeout_seconds_source": "explicit_run_timeout",
            "outer_run_timeout_required": True,
            "provider_managed_retries": 0,
            "fallbacks_allowed": False,
            "global_callbacks_allowed": False,
            "global_cache_allowed": False,
            "global_model_aliases_allowed": False,
            "global_error_log_retention_allowed": False,
            "message_logging_disabled_during_run": True,
            "external_tracing_disabled": True,
            "include_usage_requested": True,
            "usage_missing_policy": (
                "positive_request_with_all_zero_tokens_is_unavailable"
            ),
            "api_key_persisted_or_logged": False,
        },
        "entry_points": {
            "phase6_cli_enabled": True,
            "self_pilot_cli_enabled": True,
            "self_pilot_web_enabled": False,
            "eval_v2_public_runner_enabled": False,
        },
        "evaluation_boundary": {
            "campaign_registered": False,
            "public_candidate_authorized": False,
            "private_access_authorized": False,
            "online_calls_performed": False,
            "model_quality_claim_allowed": False,
            "prior_deepseek_results_inherited": False,
        },
        "official_model_reference": (
            "https://platform.claude.com/docs/en/about-claude/models/"
            "model-ids-and-versions"
        ),
        "official_model_ids_verified_at_utc": "2026-08-25T00:00:00Z",
    }


def load_public_regression_task_orders(
    path: str | Path,
    *,
    execution_channel: str | None = None,
) -> dict[int, tuple[str, ...]]:
    manifest = _load_json_object(Path(path), "public regression split manifest")
    repetitions = manifest.get("repetitions")
    channels = manifest.get("execution_channels")
    if not isinstance(repetitions, list) or not isinstance(channels, Mapping):
        raise EvalV2ContractError(
            "eval_v2_public_split_invalid", "Public split manifest 结构无效。"
        )
    allowed: set[str] | None = None
    if execution_channel is not None:
        values = channels.get(execution_channel)
        if not isinstance(values, list) or not all(isinstance(item, str) for item in values):
            raise EvalV2ContractError(
                "eval_v2_public_split_channel_invalid", "Execution channel 无效。"
            )
        allowed = set(values)
    return {
        int(item["repetition_index"]): tuple(
            task_id
            for task_id in item["task_order"]
            if allowed is None or task_id in allowed
        )
        for item in repetitions
    }


def candidate_commitment_sha256(candidate: Mapping[str, Any]) -> str:
    payload = dict(candidate)
    payload.pop("candidate_commitment_sha256", None)
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def sha256_bundle_v1(project_root: str | Path, relative_paths: Sequence[str]) -> str:
    root = Path(project_root).resolve()
    normalized = sorted(relative_paths)
    if not normalized or len(normalized) != len(set(normalized)):
        raise EvalV2ContractError(
            "eval_v2_bundle_invalid", "Bundle path scope 为空或包含重复项。"
        )
    digest = hashlib.sha256()
    for relative in normalized:
        if not isinstance(relative, str) or "\\" in relative:
            raise EvalV2ContractError(
                "eval_v2_bundle_path_invalid", "Bundle path 必须是 POSIX 相对路径。"
            )
        path = (root / relative).resolve()
        if (
            Path(relative).is_absolute()
            or ".." in Path(relative).parts
            or not path.is_relative_to(root)
            or path.is_symlink()
            or not path.is_file()
        ):
            raise EvalV2ContractError(
                "eval_v2_bundle_path_invalid", "Bundle path 越界、缺失或为 symlink。"
            )
        path_bytes = relative.encode("utf-8")
        content = path.read_bytes()
        digest.update(len(path_bytes).to_bytes(8, "big"))
        digest.update(path_bytes)
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
    return digest.hexdigest()


def _validate_public_regression_split(root: Path) -> dict[str, Any]:
    path = root / "evals" / "v2" / "public_regression_split_manifest.json"
    split = _load_json_object(path, "public regression split manifest")
    expected_fields = frozenset(
        {
            "schema_version",
            "manifest_id",
            "split",
            "public_tasks_sha256",
            "task_count",
            "execution_channels",
            "repetitions",
        }
    )
    _require_exact_fields(split, expected_fields, "public regression split manifest")
    tasks_path = root / "evals" / "v2" / "public_tasks.jsonl"
    dataset_manifest_path = root / "evals" / "v2" / "external_datasets.json"
    datasets = load_eval_v2_dataset_manifest(dataset_manifest_path)
    tasks = load_eval_v2_public_tasks(tasks_path, datasets)
    public_tasks = [
        task
        for task in tasks
        if task.split == "public_regression" and task.lifecycle_status == "ready"
    ]
    public_ids = [task.task_id for task in public_tasks]
    public_set = set(public_ids)
    if (
        split["schema_version"] != "1.0"
        or split["split"] != "public_regression"
        or split["task_count"] != 40
        or len(public_ids) != 40
        or split["public_tasks_sha256"] != _sha256_file(tasks_path)
    ):
        raise EvalV2ContractError(
            "eval_v2_public_split_invalid", "Public-regression split scope/hash 无效。"
        )
    channels = _strict_object(
        split["execution_channels"],
        frozenset({"provider_behavior", "deterministic_fault_injection"}),
        "execution_channels",
    )
    provider_ids = _unique_string_list(channels["provider_behavior"], "provider_behavior")
    fault_ids = _unique_string_list(
        channels["deterministic_fault_injection"], "deterministic_fault_injection"
    )
    expected_fault = {
        task.task_id for task in public_tasks if task.scenario in _FAULT_SCENARIOS
    }
    if (
        set(provider_ids).intersection(fault_ids)
        or set(provider_ids).union(fault_ids) != public_set
        or set(fault_ids) != expected_fault
        or len(provider_ids) != 31
        or len(fault_ids) != 9
    ):
        raise EvalV2ContractError(
            "eval_v2_public_split_channel_invalid",
            "Provider/fault execution channel 划分无效。",
        )
    repetitions = split["repetitions"]
    if not isinstance(repetitions, list) or len(repetitions) != 3:
        raise EvalV2ContractError(
            "eval_v2_public_split_repetitions_invalid", "必须预承诺三次排列。"
        )
    order_hashes: list[str] = []
    orders: list[tuple[str, ...]] = []
    seeds: list[int] = []
    for expected_index, item in enumerate(repetitions, start=1):
        value = _strict_object(
            item,
            frozenset({"repetition_index", "seed", "task_order", "task_order_sha256"}),
            "repetition",
        )
        order = tuple(_unique_string_list(value["task_order"], "task_order"))
        seed = value["seed"]
        if (
            value["repetition_index"] != expected_index
            or isinstance(seed, bool)
            or not isinstance(seed, int)
            or len(order) != 40
            or set(order) != public_set
        ):
            raise EvalV2ContractError(
                "eval_v2_public_split_repetitions_invalid",
                "Repetition index/seed/order 无效。",
            )
        expected_order = list(public_ids)
        random.Random(seed).shuffle(expected_order)
        order_hash = hashlib.sha256("\n".join(order).encode("utf-8")).hexdigest()
        if list(order) != expected_order or value["task_order_sha256"] != order_hash:
            raise EvalV2ContractError(
                "eval_v2_public_split_order_drift",
                "预承诺 seed、task order 或 order hash 不匹配。",
            )
        seeds.append(seed)
        orders.append(order)
        order_hashes.append(order_hash)
    if len(set(seeds)) != 3 or len(set(orders)) != 3:
        raise EvalV2ContractError(
            "eval_v2_public_split_repetitions_invalid",
            "三次 repetition 必须使用不同 seed 和排列。",
        )
    return {
        "manifest_id": split["manifest_id"],
        "task_order_sha256": order_hashes,
    }


def _validate_requirements_lock(path: Path, *, verify_environment: bool) -> dict[str, Any]:
    lines = [line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    packages: dict[str, str] = {}
    for line in lines:
        match = _EXACT_REQUIREMENT.fullmatch(line)
        if match is None:
            raise EvalV2ContractError(
                "eval_v2_dependency_lock_invalid", "requirements.lock 必须全部精确 pin。"
            )
        normalized = re.sub(r"[-_.]+", "-", match.group("name")).lower()
        if normalized in packages:
            raise EvalV2ContractError(
                "eval_v2_dependency_lock_invalid", "requirements.lock 包名重复。"
            )
        packages[normalized] = match.group("version")
    mismatches: list[str] = []
    if verify_environment:
        for name, expected in packages.items():
            try:
                actual = importlib.metadata.version(name)
            except importlib.metadata.PackageNotFoundError:
                mismatches.append(name + ":missing")
                continue
            if actual != expected:
                mismatches.append(name + ":version_mismatch")
        if mismatches:
            raise EvalV2ContractError(
                "eval_v2_dependency_environment_drift",
                "当前环境与 requirements.lock 不一致：" + ", ".join(mismatches),
            )
    return {
        "exact_pin_count": len(packages),
        "environment_verified": verify_environment,
        "environment_mismatch_count": len(mismatches),
        "artifact_hashes_included": False,
    }


def _load_json_object(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_constant,
        )
    except EvalV2ContractError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise EvalV2ContractError(
            "eval_v2_public_candidate_file_invalid", f"无法读取 {label}。"
        ) from exc
    if not isinstance(value, dict):
        raise EvalV2ContractError(
            "eval_v2_public_candidate_file_invalid", f"{label} 顶层必须是对象。"
        )
    return value


def _strict_object(value: Any, fields: frozenset[str], label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or set(value) != set(fields):
        raise EvalV2ContractError(
            "eval_v2_public_candidate_field_invalid", f"{label} 字段集合无效。"
        )
    return value


def _require_exact_fields(value: Mapping[str, Any], fields: frozenset[str], label: str) -> None:
    if set(value) != set(fields):
        raise EvalV2ContractError(
            "eval_v2_public_candidate_field_invalid", f"{label} 字段集合无效。"
        )


def _unique_string_list(value: Any, label: str) -> list[str]:
    if (
        not isinstance(value, list)
        or not all(isinstance(item, str) and item for item in value)
        or len(value) != len(set(value))
    ):
        raise EvalV2ContractError(
            "eval_v2_public_candidate_field_invalid", f"{label} 必须是唯一字符串列表。"
        )
    return list(value)


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise EvalV2ContractError(
                "eval_v2_public_candidate_duplicate_key", "JSON 不允许重复键。"
            )
        result[key] = value
    return result


def _reject_constant(token: str) -> None:
    raise EvalV2ContractError(
        "eval_v2_public_candidate_non_finite", f"JSON 不允许 {token}。"
    )


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()
