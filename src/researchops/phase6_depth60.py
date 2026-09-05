from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from .phase6_eval import load_phase6_tasks
from .eval_v2_freeze import validate_eval_v2_dependency_environment
from .phase6_runner import (
    Phase6RunError,
    _run_phase6_online_evaluation_impl,
    phase6_status,
)
from .phase6_source_bundle import (
    completion_telemetry_contract_bundle_sha256,
    completion_telemetry_runtime_bundle_sha256,
    phase6_depth60_source_bundle_sha256_for,
)


DEPTH60_PLAN_ID = "phase6-deepseek-depth60-v1"
DEPTH60_PLAN_SCHEMA_VERSION = "1.0"
DEPTH60_PLAN_DOMAIN = b"researchops-phase6-deepseek-depth60-plan-v1\0"
DEPTH60_PLAN_PATH = Path("evals/phase6_deepseek_depth60_plan.json")
DEPTH60_SUCCESSOR_PLAN_ID = "phase6-deepseek-depth60-v2"
DEPTH60_SUCCESSOR_PLAN_SCHEMA_VERSION = "2.0"
DEPTH60_SUCCESSOR_PLAN_DOMAIN = (
    b"researchops-phase6-deepseek-depth60-successor-plan-v2\0"
)
DEPTH60_SUCCESSOR_PLAN_PATH = Path(
    "evals/phase6_deepseek_depth60_plan_v2.json"
)
DEPTH60_SUCCESSOR_V3_PLAN_ID = "phase6-deepseek-depth60-v3"
DEPTH60_SUCCESSOR_V3_PLAN_SCHEMA_VERSION = "3.0"
DEPTH60_SUCCESSOR_V3_PLAN_DOMAIN = (
    b"researchops-phase6-deepseek-depth60-successor-plan-v3\0"
)
DEPTH60_SUCCESSOR_V3_PLAN_PATH = Path(
    "evals/phase6_deepseek_depth60_plan_v3.json"
)
DEPTH60_SUCCESSOR_V4_PLAN_ID = "phase6-deepseek-depth60-v4"
DEPTH60_SUCCESSOR_V4_PLAN_SCHEMA_VERSION = "4.0"
DEPTH60_SUCCESSOR_V4_PLAN_DOMAIN = (
    b"researchops-phase6-deepseek-depth60-successor-plan-v4\0"
)
DEPTH60_SUCCESSOR_V4_PLAN_PATH = Path(
    "evals/phase6_deepseek_depth60_plan_v4.json"
)
DEPTH60_SUCCESSOR_V5_PLAN_ID = "phase6-deepseek-depth60-v5"
DEPTH60_SUCCESSOR_V5_PLAN_SCHEMA_VERSION = "5.0"
DEPTH60_SUCCESSOR_V5_PLAN_DOMAIN = (
    b"researchops-phase6-deepseek-depth60-successor-plan-v5\0"
)
DEPTH60_SUCCESSOR_V5_PLAN_PATH = Path(
    "evals/phase6_deepseek_depth60_plan_v5.json"
)
# The historical commitment is an assertion about a commit, not about HEAD.
# These literals exist so the successor cannot be validated while the history
# it claims to supersede has been altered or removed.
DEPTH60_HISTORICAL_SOURCE_BUNDLE_SHA256 = (
    "914acbe89f4d99240aa653ecfe07fc0a2c129d08aa6abee9eb401e5f9d7a8d84"
)
DEPTH60_HISTORICAL_PLAN_COMMITMENT_SHA256 = (
    "8019ef294b5028ab4e44c006f01e02bddb5a3b67b1ed88b84945bf37e75c216e"
)
DEPTH60_V2_PLAN_BYTES = 2170
DEPTH60_V2_PLAN_FILE_SHA256 = (
    "fc4ca5cc2131efb36d82f1d739f65ad2a026e1c7534f0da9c873942a40c1002f"
)
DEPTH60_V2_PLAN_COMMITMENT_SHA256 = (
    "3077a55e09f3f2137155a68d96a5bda60d8553cc9b5dd36ca83d33bbbc3dcf7e"
)
DEPTH60_V2_SOURCE_BUNDLE_SHA256 = (
    "cd46dc03771fc0ebca7ea50798fe2b32fa76248882881f7249c777cd3270ab25"
)
DEPTH60_V3_PLAN_BYTES = 2850
DEPTH60_V3_PLAN_FILE_SHA256 = (
    "5602f940a8627b9c785a1b785d757119a616050ebbc2e31e9dd26aacf448c05e"
)
DEPTH60_V3_PLAN_COMMITMENT_SHA256 = (
    "979202e96a5304ad1ba73c54e55f0d38f80baedf8278e37ea8535bb5560ce6af"
)
DEPTH60_V3_SOURCE_BUNDLE_SHA256 = (
    "f5af13c7475f7c152a3cbe2053c7b0f81f6d4b15034e8a58786ab9e7124a19bf"
)
DEPTH60_V3_RUNTIME_BUNDLE_SHA256 = (
    "694c948a1d79fd38532304846577a94a4c75ca1410dd97c363df71a4ba944a63"
)
DEPTH60_V3_CONTRACT_BUNDLE_SHA256 = (
    "c9c54c425932770254b9f460d7ab5120401ba02f6802626fb7399d3333700011"
)
DEPTH60_V4_PLAN_BYTES = 3087
DEPTH60_V4_PLAN_FILE_SHA256 = (
    "ae961e069afa9c842c294f7fb6951e0cf3a4ad86dfcdd16cb96a2c264c232956"
)
DEPTH60_V4_PLAN_COMMITMENT_SHA256 = (
    "c36dc0dd0487aa350dc2bd636b45bb494381e0c732c80be7b410be4b9beda612"
)
DEPTH60_V4_SOURCE_BUNDLE_SHA256 = (
    "4bd5a48be256b124a7c297f5f98ef4bbadd09df07a038067d7aca211e1fc772c"
)
DEPTH60_V4_RUNTIME_BUNDLE_SHA256 = (
    "b0e6f0feb3af416ca73f04df9e8c1cc7f10b5c700e2a20c2a3a7c688273f42c2"
)
DEPTH60_V4_CONTRACT_BUNDLE_SHA256 = (
    "1e4028e3bc9c128391a4a73c6753bb5da5e9d19079cbf1068d9acb64980fa56f"
)
DEPTH60_TASK_IDS = tuple(f"P6-DEV-{index:03d}" for index in range(1, 61))
_AUTHORIZATION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_SHA256 = re.compile(r"^[a-f0-9]{64}$")
_OFFICIAL_SOURCES = (
    {
        "source_id": "deepseek_models_and_pricing",
        "url": "https://api-docs.deepseek.com/zh-cn/quick_start/pricing/",
        "captured_at_utc": "2026-08-31T13:49:09.077Z",
        "decoded_utf8_bytes": 23354,
        "sha256": "899affbdbc33d0be620d8dea59e86f5036c11b5410b14d060b8d2874c74f38e5",
    },
    {
        "source_id": "deepseek_change_log",
        "url": "https://api-docs.deepseek.com/updates/",
        "captured_at_utc": "2026-08-31T13:49:09.077Z",
        "decoded_utf8_bytes": 48149,
        "sha256": "9f0e83b23b4e5aaf47b973a222821bb033d0541c8eae67d03a5ab73858d4713a",
    },
    {
        "source_id": "deepseek_models_list_reference",
        "url": "https://api-docs.deepseek.com/api/list-models/",
        "captured_at_utc": "2026-08-31T13:49:09.077Z",
        "decoded_utf8_bytes": 29721,
        "sha256": "29cf290efb280a177f5b65e2f73ca3ec58486da1462912ad91f9211dfd482f84",
    },
)


def build_depth60_component_hashes(
    project_root: str | Path, algorithm: str | None = None
) -> dict[str, str]:
    """Component hashes for a Depth-60 plan.

    ``algorithm=None`` keeps the v1 source-bundle digest, so every existing
    caller — including the historical plan validator and the runtime re-check —
    is behaviorally unaffected.
    """

    root = Path(project_root).resolve()
    return {
        "source_bundle_sha256": phase6_depth60_source_bundle_sha256_for(
            root, algorithm
        ),
        "phase6_tasks_sha256": _sha256_file(root / "evals/phase6_agent_tasks.jsonl"),
        "phase6_splits_sha256": _sha256_file(root / "evals/phase6_splits.json"),
        "requirements_lock_sha256": _sha256_file(root / "requirements.lock"),
        "pyproject_sha256": _sha256_file(root / "pyproject.toml"),
        "synthetic_trial_csv_sha256": _sha256_file(
            root / "data/synthetic_trial.csv"
        ),
        "synthetic_trial_design_sha256": _sha256_file(
            root / "data/synthetic_trial_design.json"
        ),
        "phase3_analysis_bundle_sha256": _sha256_file(
            root / "artifacts/phase3/analysis_bundle.json"
        ),
        "phase3_effect_estimates_png_sha256": _sha256_file(
            root / "artifacts/phase3/effect_estimates.png"
        ),
    }


def build_depth60_component_hashes_v3(
    project_root: str | Path,
) -> dict[str, str]:
    """Bind the v2 ResearchOps closure plus both telemetry sibling bundles."""

    root = Path(project_root).resolve()
    components = build_depth60_component_hashes(root, "v2")
    components["completion_telemetry_runtime_bundle_sha256"] = (
        completion_telemetry_runtime_bundle_sha256(root)
    )
    components["completion_telemetry_contract_bundle_sha256"] = (
        completion_telemetry_contract_bundle_sha256(root)
    )
    return components


def build_depth60_component_hashes_v4(
    project_root: str | Path,
) -> dict[str, str]:
    """Bind the current v2 closure and the hardened telemetry bundles."""

    return build_depth60_component_hashes_v3(project_root)


def build_depth60_component_hashes_v5(
    project_root: str | Path,
) -> dict[str, str]:
    """Bind the first-live validation control plane and contract successors."""

    return build_depth60_component_hashes_v4(project_root)


def depth60_plan_commitment_sha256(plan: Mapping[str, Any]) -> str:
    body = dict(plan)
    body.pop("plan_commitment_sha256", None)
    payload = json.dumps(
        body,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(DEPTH60_PLAN_DOMAIN + payload).hexdigest()


def depth60_successor_v3_plan_commitment_sha256(
    plan: Mapping[str, Any],
) -> str:
    body = dict(plan)
    body.pop("plan_commitment_sha256", None)
    payload = json.dumps(
        body,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(DEPTH60_SUCCESSOR_V3_PLAN_DOMAIN + payload).hexdigest()


def depth60_successor_v4_plan_commitment_sha256(
    plan: Mapping[str, Any],
) -> str:
    body = dict(plan)
    body.pop("plan_commitment_sha256", None)
    payload = json.dumps(
        body,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(DEPTH60_SUCCESSOR_V4_PLAN_DOMAIN + payload).hexdigest()


def depth60_successor_v5_plan_commitment_sha256(
    plan: Mapping[str, Any],
) -> str:
    body = dict(plan)
    body.pop("plan_commitment_sha256", None)
    payload = json.dumps(
        body,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(DEPTH60_SUCCESSOR_V5_PLAN_DOMAIN + payload).hexdigest()


def build_depth60_successor_plan_v3(
    project_root: str | Path,
    *,
    locked_at_utc: str,
) -> dict[str, Any]:
    """Build, but do not write, a v3 source-integrity successor plan."""

    if not isinstance(locked_at_utc, str) or not locked_at_utc.endswith("Z"):
        raise Phase6RunError(
            "phase6_depth60_v3_locked_at_invalid",
            "Depth-60 v3 locked_at_utc 必须是以 Z 结尾的 UTC 字符串。",
        )
    root = Path(project_root).resolve()
    body: dict[str, Any] = {
        "schema_version": DEPTH60_SUCCESSOR_V3_PLAN_SCHEMA_VERSION,
        "plan_id": DEPTH60_SUCCESSOR_V3_PLAN_ID,
        "status": "locked_offline_not_run",
        "locked_at_utc": locked_at_utc,
        "evaluation_scope": "source_integrity_commitment_only",
        "source_bundle_algorithm": "v2",
        "component_hash_algorithms": {
            "source_bundle_sha256": "v2",
            "completion_telemetry_runtime_bundle_sha256": (
                "completion_telemetry_runtime_bundle_v1"
            ),
            "completion_telemetry_contract_bundle_sha256": (
                "completion_telemetry_contract_bundle_v1"
            ),
        },
        "component_hashes": build_depth60_component_hashes_v3(root),
        "supersedes": {
            "plan_id": DEPTH60_SUCCESSOR_PLAN_ID,
            "plan_commitment_sha256": DEPTH60_V2_PLAN_COMMITMENT_SHA256,
            "source_bundle_sha256": DEPTH60_V2_SOURCE_BUNDLE_SHA256,
            "source_bundle_algorithm": "v2",
            "historical_plan_relative_path": (
                DEPTH60_SUCCESSOR_PLAN_PATH.as_posix()
            ),
            "historical_plan_bytes": DEPTH60_V2_PLAN_BYTES,
            "historical_plan_sha256": DEPTH60_V2_PLAN_FILE_SHA256,
            "historical_commitment_preserved": True,
            "historical_run_superseded": False,
        },
        "authorization_boundary": {
            "plan_alone_authorizes_online_run": False,
            "online_execution_authorized": False,
            "usable_as_runtime_binding": False,
            "supersedes_historical_online_authorization": False,
        },
        "claim_boundary": {
            "model_quality_claim_allowed": False,
            "reproduces_historical_depth60_run": False,
            "historical_result_revalidated": False,
            "source_integrity_scope": (
                "current_tree_plus_completion_telemetry_runtime_and_contract_bundles"
            ),
        },
    }
    plan = dict(body)
    plan["plan_commitment_sha256"] = (
        depth60_successor_v3_plan_commitment_sha256(plan)
    )
    return plan


def build_depth60_successor_plan_v4(
    project_root: str | Path,
    *,
    locked_at_utc: str,
) -> dict[str, Any]:
    """Build, but do not write, the non-executable v4 integrity successor."""

    if not isinstance(locked_at_utc, str) or not locked_at_utc.endswith("Z"):
        raise Phase6RunError(
            "phase6_depth60_v4_locked_at_invalid",
            "Depth-60 v4 locked_at_utc 必须是以 Z 结尾的 UTC 字符串。",
        )
    root = Path(project_root).resolve()
    _validate_frozen_depth60_v3_lineage(root)
    body: dict[str, Any] = {
        "schema_version": DEPTH60_SUCCESSOR_V4_PLAN_SCHEMA_VERSION,
        "plan_id": DEPTH60_SUCCESSOR_V4_PLAN_ID,
        "status": "locked_offline_not_run",
        "locked_at_utc": locked_at_utc,
        "evaluation_scope": "source_integrity_commitment_only",
        "source_bundle_algorithm": "v2",
        "component_hash_algorithms": {
            "source_bundle_sha256": "v2",
            "completion_telemetry_runtime_bundle_sha256": (
                "completion_telemetry_runtime_bundle_v1"
            ),
            "completion_telemetry_contract_bundle_sha256": (
                "completion_telemetry_contract_bundle_v1"
            ),
        },
        "component_hashes": build_depth60_component_hashes_v4(root),
        "supersedes": {
            "plan_id": DEPTH60_SUCCESSOR_V3_PLAN_ID,
            "plan_commitment_sha256": DEPTH60_V3_PLAN_COMMITMENT_SHA256,
            "source_bundle_sha256": DEPTH60_V3_SOURCE_BUNDLE_SHA256,
            "completion_telemetry_runtime_bundle_sha256": (
                DEPTH60_V3_RUNTIME_BUNDLE_SHA256
            ),
            "completion_telemetry_contract_bundle_sha256": (
                DEPTH60_V3_CONTRACT_BUNDLE_SHA256
            ),
            "source_bundle_algorithm": "v2",
            "historical_plan_relative_path": (
                DEPTH60_SUCCESSOR_V3_PLAN_PATH.as_posix()
            ),
            "historical_plan_bytes": DEPTH60_V3_PLAN_BYTES,
            "historical_plan_sha256": DEPTH60_V3_PLAN_FILE_SHA256,
            "historical_commitment_preserved": True,
            "historical_run_superseded": False,
        },
        "authorization_boundary": {
            "plan_alone_authorizes_online_run": False,
            "online_execution_authorized": False,
            "usable_as_runtime_binding": False,
            "supersedes_historical_online_authorization": False,
        },
        "claim_boundary": {
            "model_quality_claim_allowed": False,
            "reproduces_historical_depth60_run": False,
            "historical_result_revalidated": False,
            "source_integrity_scope": (
                "current_tree_plus_completion_telemetry_runtime_and_contract_bundles"
            ),
        },
    }
    plan = dict(body)
    plan["plan_commitment_sha256"] = (
        depth60_successor_v4_plan_commitment_sha256(plan)
    )
    return plan


def build_depth60_successor_plan_v5(
    project_root: str | Path,
    *,
    locked_at_utc: str,
) -> dict[str, Any]:
    """Build, but do not write, the non-executable first-live integrity successor."""

    if not isinstance(locked_at_utc, str) or not locked_at_utc.endswith("Z"):
        raise Phase6RunError(
            "phase6_depth60_v5_locked_at_invalid",
            "Depth-60 v5 locked_at_utc 必须是以 Z 结尾的 UTC 字符串。",
        )
    root = Path(project_root).resolve()
    _validate_frozen_depth60_v4_lineage(root)
    body: dict[str, Any] = {
        "schema_version": DEPTH60_SUCCESSOR_V5_PLAN_SCHEMA_VERSION,
        "plan_id": DEPTH60_SUCCESSOR_V5_PLAN_ID,
        "status": "locked_offline_not_run",
        "locked_at_utc": locked_at_utc,
        "evaluation_scope": "source_integrity_commitment_only",
        "source_bundle_algorithm": "v2",
        "component_hash_algorithms": {
            "source_bundle_sha256": "v2",
            "completion_telemetry_runtime_bundle_sha256": (
                "completion_telemetry_runtime_bundle_v1"
            ),
            "completion_telemetry_contract_bundle_sha256": (
                "completion_telemetry_contract_bundle_v1"
            ),
        },
        "component_hashes": build_depth60_component_hashes_v5(root),
        "supersedes": {
            "plan_id": DEPTH60_SUCCESSOR_V4_PLAN_ID,
            "plan_commitment_sha256": DEPTH60_V4_PLAN_COMMITMENT_SHA256,
            "source_bundle_sha256": DEPTH60_V4_SOURCE_BUNDLE_SHA256,
            "completion_telemetry_runtime_bundle_sha256": (
                DEPTH60_V4_RUNTIME_BUNDLE_SHA256
            ),
            "completion_telemetry_contract_bundle_sha256": (
                DEPTH60_V4_CONTRACT_BUNDLE_SHA256
            ),
            "source_bundle_algorithm": "v2",
            "historical_plan_relative_path": (
                DEPTH60_SUCCESSOR_V4_PLAN_PATH.as_posix()
            ),
            "historical_plan_bytes": DEPTH60_V4_PLAN_BYTES,
            "historical_plan_sha256": DEPTH60_V4_PLAN_FILE_SHA256,
            "historical_commitment_preserved": True,
            "historical_run_superseded": False,
        },
        "authorization_boundary": {
            "plan_alone_authorizes_online_run": False,
            "online_execution_authorized": False,
            "usable_as_runtime_binding": False,
            "supersedes_historical_online_authorization": False,
        },
        "claim_boundary": {
            "model_quality_claim_allowed": False,
            "reproduces_historical_depth60_run": False,
            "historical_result_revalidated": False,
            "source_integrity_scope": (
                "enumerated_source_dependency_closure_plus_explicit_completion_telemetry_"
                "runtime_and_contract_bundles"
            ),
        },
    }
    plan = dict(body)
    plan["plan_commitment_sha256"] = (
        depth60_successor_v5_plan_commitment_sha256(plan)
    )
    return plan


def validate_phase6_depth60_plan(
    project_root: str | Path,
    plan_path: str | Path = DEPTH60_PLAN_PATH,
) -> dict[str, Any]:
    root = Path(project_root).resolve()
    resolved_plan = Path(plan_path)
    if not resolved_plan.is_absolute():
        resolved_plan = root / resolved_plan
    resolved_plan = resolved_plan.resolve()
    if resolved_plan == (root / DEPTH60_SUCCESSOR_PLAN_PATH).resolve():
        return _validate_depth60_successor_plan(root, resolved_plan)
    if resolved_plan == (root / DEPTH60_SUCCESSOR_V3_PLAN_PATH).resolve():
        return _validate_depth60_successor_v3_plan(root, resolved_plan)
    if resolved_plan == (root / DEPTH60_SUCCESSOR_V4_PLAN_PATH).resolve():
        return _validate_depth60_successor_v4_plan(root, resolved_plan)
    if resolved_plan == (root / DEPTH60_SUCCESSOR_V5_PLAN_PATH).resolve():
        return _validate_depth60_successor_v5_plan(root, resolved_plan)
    expected_plan = (root / DEPTH60_PLAN_PATH).resolve()
    if resolved_plan != expected_plan:
        raise Phase6RunError(
            "phase6_depth60_plan_path_invalid",
            "Depth-60 plan 必须使用仓库中的固定路径。",
        )
    plan = _load_json_object(resolved_plan)
    _require_exact_fields(
        plan,
        {
            "schema_version",
            "plan_id",
            "status",
            "locked_at_utc",
            "evaluation_scope",
            "provider",
            "selection",
            "budget",
            "official_sources",
            "component_hashes",
            "authorization_boundary",
            "claim_boundary",
            "plan_commitment_sha256",
        },
        "depth60 plan",
    )
    if (
        plan.get("schema_version") != DEPTH60_PLAN_SCHEMA_VERSION
        or plan.get("plan_id") != DEPTH60_PLAN_ID
        or plan.get("status") != "locked_offline_not_run"
        or plan.get("evaluation_scope")
        != "repo_visible_development_provider_behavior_depth"
        or not isinstance(plan.get("locked_at_utc"), str)
        or not plan["locked_at_utc"].endswith("Z")
    ):
        raise Phase6RunError(
            "phase6_depth60_plan_invalid", "Depth-60 plan identity/status 无效。"
        )
    expected_provider = {
        "provider_id": "deepseek",
        "model_alias": "deepseek-v4-flash",
        "official_version_snapshot": "DeepSeek-V4-Flash-0731",
        "model_alias_mutable": True,
        "transport_id": "openai_compatible_responses",
        "split": "development",
        "max_cases": 60,
        "max_turns": 8,
        "case_timeout_seconds": 120,
        "max_output_tokens": 2000,
        "sequential_execution": True,
        "client_retries": 0,
        "resume": False,
    }
    if plan.get("provider") != expected_provider:
        raise Phase6RunError(
            "phase6_depth60_provider_invalid", "Depth-60 provider 配置发生漂移。"
        )
    expected_selection = {
        "corpus_task_count": 64,
        "development_task_count": 60,
        "holdout_task_count": 4,
        "selected_task_ids": list(DEPTH60_TASK_IDS),
        "selected_task_count": 60,
        "legacy_development_task_count": 16,
        "new_frozen_extension_task_count": 44,
        "holdout_executed": False,
        "selection_used_prior_online_results": False,
        "post_run_prompt_or_scorer_tuning_allowed": False,
    }
    if plan.get("selection") != expected_selection:
        raise Phase6RunError(
            "phase6_depth60_selection_invalid", "Depth-60 task scope 发生漂移。"
        )
    expected_budget = {
        "currency": "CNY",
        "local_observed_cost_stop_cny": "6.000000",
        "pricing_snapshot_date": "2026-08-31",
        "pricing_source_url": (
            "https://api-docs.deepseek.com/zh-cn/quick_start/pricing/"
        ),
        "estimate_method": "peak_hours_all_input_cache_miss",
        "input_cache_miss_cny_per_million": "3.000000",
        "output_cny_per_million": "9.000000",
        "total_input_tokens_cap": 750000,
        "total_output_tokens_cap": 350000,
        "total_requests_cap": 450,
        "total_timeout_seconds": 5400,
        "provider_billing_hard_cap": False,
        "actual_provider_bill_known": False,
        "strict_provider_hard_cap": False,
        "enforcement_semantics": "pre_case_reserve_plus_post_case_observed_stop",
        "single_case_overshoot_possible": True,
    }
    if plan.get("budget") != expected_budget:
        raise Phase6RunError(
            "phase6_depth60_budget_invalid", "Depth-60 budget/price 发生漂移。"
        )
    if plan.get("official_sources") != list(_OFFICIAL_SOURCES):
        raise Phase6RunError(
            "phase6_depth60_source_commitment_invalid",
            "Depth-60 官方模型/价格来源 commitment 无效。",
        )
    tasks = load_phase6_tasks(
        root / "evals/phase6_agent_tasks.jsonl",
        root / "evals/phase6_splits.json",
    )
    selected_ids = [task.task_id for task in tasks if task.split == "development"]
    holdout_ids = [task.task_id for task in tasks if task.split == "holdout"]
    if selected_ids != list(DEPTH60_TASK_IDS) or holdout_ids != [
        "P6-HOLD-001",
        "P6-HOLD-002",
        "P6-HOLD-003",
        "P6-HOLD-004",
    ]:
        raise Phase6RunError(
            "phase6_depth60_corpus_scope_invalid",
            "Depth-60 corpus 顺序或 holdout 隔离发生漂移。",
        )
    actual_components = build_depth60_component_hashes(root)
    if plan.get("component_hashes") != actual_components:
        raise Phase6RunError(
            "phase6_depth60_component_drift",
            "Depth-60 source/corpus/data/dependency component drift。",
        )
    expected_authorization = {
        "plan_alone_authorizes_online_run": False,
        "fresh_single_use_authorization_required": True,
        "authorization_id_and_expiry_required": True,
        "single_use_receipt_namespace": "artifacts/phase6_deepseek_depth60",
        "network_calls_performed": 0,
        "model_calls_performed": 0,
    }
    if plan.get("authorization_boundary") != expected_authorization:
        raise Phase6RunError(
            "phase6_depth60_authorization_boundary_invalid",
            "Depth-60 authorization boundary 无效。",
        )
    expected_claims = {
        "model_quality_claim_allowed": False,
        "private_holdout_claim_allowed": False,
        "unknown_distribution_generalization_claim_allowed": False,
        "production_sla_claim_allowed": False,
        "cross_provider_claim_allowed": False,
        "result_attributed_to_model_alone": False,
        "result_attribution": "deepseek_plus_frozen_control_plane",
    }
    if plan.get("claim_boundary") != expected_claims:
        raise Phase6RunError(
            "phase6_depth60_claim_boundary_invalid",
            "Depth-60 claim boundary 无效。",
        )
    commitment = plan.get("plan_commitment_sha256")
    if (
        not isinstance(commitment, str)
        or _SHA256.fullmatch(commitment) is None
        or commitment != depth60_plan_commitment_sha256(plan)
    ):
        raise Phase6RunError(
            "phase6_depth60_commitment_invalid",
            "Depth-60 plan commitment 无效。",
        )
    return {
        "status": "valid",
        "plan_id": DEPTH60_PLAN_ID,
        "plan_commitment_sha256": commitment,
        "selected_task_count": 60,
        "holdout_executed": False,
        "network_calls": 0,
        "model_calls": 0,
        "plan": plan,
    }


def _validate_depth60_successor_plan(
    root: Path, resolved_plan: Path
) -> dict[str, Any]:
    """Validate the successor source-integrity plan.

    The successor commits the current enumerated components under the v2 bundle
    algorithm. It is deliberately not an online authorization: it carries no
    provider, budget, or selection, and :func:`run_phase6_depth60_online`
    refuses it outright.
    """

    plan = _load_json_object(resolved_plan)
    _require_exact_fields(
        plan,
        {
            "schema_version",
            "plan_id",
            "status",
            "locked_at_utc",
            "evaluation_scope",
            "source_bundle_algorithm",
            "component_hashes",
            "supersedes",
            "authorization_boundary",
            "claim_boundary",
            "plan_commitment_sha256",
        },
        "depth60 successor plan",
    )
    if (
        plan.get("schema_version") != DEPTH60_SUCCESSOR_PLAN_SCHEMA_VERSION
        or plan.get("plan_id") != DEPTH60_SUCCESSOR_PLAN_ID
        or plan.get("status") != "locked_offline_not_run"
        or plan.get("evaluation_scope") != "source_integrity_commitment_only"
        or plan.get("source_bundle_algorithm") != "v2"
        or not isinstance(plan.get("locked_at_utc"), str)
        or not plan["locked_at_utc"].endswith("Z")
    ):
        raise Phase6RunError(
            "phase6_depth60_successor_plan_invalid",
            "Depth-60 后继 plan identity/status/algorithm 无效。",
        )

    # Preserve, do not overwrite: refuse to validate a successor whose claimed
    # predecessor is no longer on disk in its committed form.
    historical_path = (root / DEPTH60_PLAN_PATH).resolve()
    if historical_path.is_symlink() or not historical_path.is_file():
        raise Phase6RunError(
            "phase6_depth60_historical_commitment_missing",
            "历史 Depth-60 plan 缺失，后继 plan 不得生效。",
        )
    historical = _load_json_object(historical_path)
    historical_components = historical.get("component_hashes")
    computed_historical_commitment = depth60_plan_commitment_sha256(historical)
    if (
        historical.get("plan_id") != DEPTH60_PLAN_ID
        or historical.get("plan_commitment_sha256")
        != DEPTH60_HISTORICAL_PLAN_COMMITMENT_SHA256
        or computed_historical_commitment
        != DEPTH60_HISTORICAL_PLAN_COMMITMENT_SHA256
        or not isinstance(historical_components, Mapping)
        or historical_components.get("source_bundle_sha256")
        != DEPTH60_HISTORICAL_SOURCE_BUNDLE_SHA256
    ):
        raise Phase6RunError(
            "phase6_depth60_historical_commitment_missing",
            "历史 Depth-60 commitment 已被改写，后继 plan 不得生效。",
        )

    if plan.get("supersedes") != {
        "plan_id": DEPTH60_PLAN_ID,
        "plan_commitment_sha256": DEPTH60_HISTORICAL_PLAN_COMMITMENT_SHA256,
        "source_bundle_sha256": DEPTH60_HISTORICAL_SOURCE_BUNDLE_SHA256,
        "source_bundle_algorithm": "v1",
        "historical_plan_relative_path": DEPTH60_PLAN_PATH.as_posix(),
        "historical_commitment_preserved": True,
        "historical_run_superseded": False,
    }:
        raise Phase6RunError(
            "phase6_depth60_successor_lineage_invalid",
            "Depth-60 后继 plan 的 supersedes 血缘块不正确。",
        )

    if plan.get("authorization_boundary") != {
        "plan_alone_authorizes_online_run": False,
        "online_execution_authorized": False,
        "usable_as_runtime_binding": False,
        "supersedes_historical_online_authorization": False,
    }:
        raise Phase6RunError(
            "phase6_depth60_successor_plan_invalid",
            "Depth-60 后继 plan 的 authorization_boundary 漂移。",
        )
    if plan.get("claim_boundary") != {
        "model_quality_claim_allowed": False,
        "reproduces_historical_depth60_run": False,
        "historical_result_revalidated": False,
        "source_integrity_scope": "current_tree_only",
    }:
        raise Phase6RunError(
            "phase6_depth60_successor_plan_invalid",
            "Depth-60 后继 plan 的 claim_boundary 漂移。",
        )

    actual_components = build_depth60_component_hashes(root, "v2")
    if plan.get("component_hashes") != actual_components:
        raise Phase6RunError(
            "phase6_depth60_successor_component_drift",
            "Depth-60 后继 plan 的 component 与当前树不一致。",
            not_run=True,
        )
    if plan["component_hashes"]["source_bundle_sha256"] == (
        DEPTH60_HISTORICAL_SOURCE_BUNDLE_SHA256
    ):
        raise Phase6RunError(
            "phase6_depth60_successor_lineage_invalid",
            "后继 source bundle 不得与历史 commitment 相同。",
        )

    commitment = plan.get("plan_commitment_sha256")
    body = {
        key: value
        for key, value in plan.items()
        if key != "plan_commitment_sha256"
    }
    payload = json.dumps(
        body,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    computed = hashlib.sha256(
        DEPTH60_SUCCESSOR_PLAN_DOMAIN + payload
    ).hexdigest()
    if (
        not isinstance(commitment, str)
        or _SHA256.fullmatch(commitment) is None
        or commitment != computed
    ):
        raise Phase6RunError(
            "phase6_depth60_successor_plan_invalid",
            "Depth-60 后继 plan commitment 无效。",
        )
    return {
        "status": "valid",
        "plan_id": DEPTH60_SUCCESSOR_PLAN_ID,
        "plan_commitment_sha256": commitment,
        "source_bundle_algorithm": "v2",
        "supersedes_plan_id": DEPTH60_PLAN_ID,
        "online_execution_authorized": False,
        "network_calls": 0,
        "model_calls": 0,
        "plan": plan,
    }


def _validate_frozen_depth60_v2_lineage(root: Path) -> dict[str, Any]:
    path = (root / DEPTH60_SUCCESSOR_PLAN_PATH).resolve()
    lexical = root / DEPTH60_SUCCESSOR_PLAN_PATH
    if lexical.is_symlink() or not path.is_file() or not path.is_relative_to(root):
        raise Phase6RunError(
            "phase6_depth60_v2_lineage_invalid",
            "冻结的 Depth-60 v2 plan 缺失、逃逸或是 symlink。",
        )
    try:
        payload = path.read_bytes()
    except OSError as exc:
        raise Phase6RunError(
            "phase6_depth60_v2_lineage_invalid",
            "无法读取冻结的 Depth-60 v2 plan。",
        ) from exc
    if (
        len(payload) != DEPTH60_V2_PLAN_BYTES
        or hashlib.sha256(payload).hexdigest() != DEPTH60_V2_PLAN_FILE_SHA256
    ):
        raise Phase6RunError(
            "phase6_depth60_v2_lineage_invalid",
            "冻结的 Depth-60 v2 plan 字节 commitment 不匹配。",
        )
    plan = _load_json_object(path)
    _require_exact_fields(
        plan,
        {
            "schema_version",
            "plan_id",
            "status",
            "locked_at_utc",
            "evaluation_scope",
            "source_bundle_algorithm",
            "component_hashes",
            "supersedes",
            "authorization_boundary",
            "claim_boundary",
            "plan_commitment_sha256",
        },
        "depth60 frozen v2 plan",
    )
    body = {key: value for key, value in plan.items() if key != "plan_commitment_sha256"}
    computed = hashlib.sha256(
        DEPTH60_SUCCESSOR_PLAN_DOMAIN
        + json.dumps(
            body,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()
    components = plan.get("component_hashes")
    if (
        plan.get("schema_version") != DEPTH60_SUCCESSOR_PLAN_SCHEMA_VERSION
        or plan.get("plan_id") != DEPTH60_SUCCESSOR_PLAN_ID
        or plan.get("status") != "locked_offline_not_run"
        or plan.get("evaluation_scope") != "source_integrity_commitment_only"
        or plan.get("source_bundle_algorithm") != "v2"
        or plan.get("plan_commitment_sha256")
        != DEPTH60_V2_PLAN_COMMITMENT_SHA256
        or computed != DEPTH60_V2_PLAN_COMMITMENT_SHA256
        or not isinstance(components, Mapping)
        or components.get("source_bundle_sha256")
        != DEPTH60_V2_SOURCE_BUNDLE_SHA256
    ):
        raise Phase6RunError(
            "phase6_depth60_v2_lineage_invalid",
            "冻结的 Depth-60 v2 plan 全体字段 commitment 无效。",
        )
    if plan.get("supersedes") != {
        "plan_id": DEPTH60_PLAN_ID,
        "plan_commitment_sha256": DEPTH60_HISTORICAL_PLAN_COMMITMENT_SHA256,
        "source_bundle_sha256": DEPTH60_HISTORICAL_SOURCE_BUNDLE_SHA256,
        "source_bundle_algorithm": "v1",
        "historical_plan_relative_path": DEPTH60_PLAN_PATH.as_posix(),
        "historical_commitment_preserved": True,
        "historical_run_superseded": False,
    } or plan.get("authorization_boundary") != {
        "plan_alone_authorizes_online_run": False,
        "online_execution_authorized": False,
        "usable_as_runtime_binding": False,
        "supersedes_historical_online_authorization": False,
    } or plan.get("claim_boundary") != {
        "model_quality_claim_allowed": False,
        "reproduces_historical_depth60_run": False,
        "historical_result_revalidated": False,
        "source_integrity_scope": "current_tree_only",
    }:
        raise Phase6RunError(
            "phase6_depth60_v2_lineage_invalid",
            "冻结的 Depth-60 v2 lineage 或边界字段无效。",
        )

    historical_lexical = root / DEPTH60_PLAN_PATH
    historical_path = historical_lexical.resolve()
    if (
        historical_lexical.is_symlink()
        or not historical_path.is_file()
        or not historical_path.is_relative_to(root)
    ):
        raise Phase6RunError(
            "phase6_depth60_v2_lineage_invalid",
            "冻结的 Depth-60 v1 predecessor 缺失、逃逸或是 symlink。",
        )
    historical = _load_json_object(historical_path)
    historical_components = historical.get("component_hashes")
    if (
        historical.get("plan_id") != DEPTH60_PLAN_ID
        or historical.get("plan_commitment_sha256")
        != DEPTH60_HISTORICAL_PLAN_COMMITMENT_SHA256
        or depth60_plan_commitment_sha256(historical)
        != DEPTH60_HISTORICAL_PLAN_COMMITMENT_SHA256
        or not isinstance(historical_components, Mapping)
        or historical_components.get("source_bundle_sha256")
        != DEPTH60_HISTORICAL_SOURCE_BUNDLE_SHA256
    ):
        raise Phase6RunError(
            "phase6_depth60_v2_lineage_invalid",
            "冻结的 Depth-60 v1 predecessor commitment 无效。",
        )
    return plan


def _validate_depth60_successor_v3_plan(
    root: Path, resolved_plan: Path
) -> dict[str, Any]:
    plan = _load_json_object(resolved_plan)
    _require_exact_fields(
        plan,
        {
            "schema_version",
            "plan_id",
            "status",
            "locked_at_utc",
            "evaluation_scope",
            "source_bundle_algorithm",
            "component_hash_algorithms",
            "component_hashes",
            "supersedes",
            "authorization_boundary",
            "claim_boundary",
            "plan_commitment_sha256",
        },
        "depth60 v3 successor plan",
    )
    if (
        plan.get("schema_version") != DEPTH60_SUCCESSOR_V3_PLAN_SCHEMA_VERSION
        or plan.get("plan_id") != DEPTH60_SUCCESSOR_V3_PLAN_ID
        or plan.get("status") != "locked_offline_not_run"
        or plan.get("evaluation_scope") != "source_integrity_commitment_only"
        or plan.get("source_bundle_algorithm") != "v2"
        or not isinstance(plan.get("locked_at_utc"), str)
        or not plan["locked_at_utc"].endswith("Z")
    ):
        raise Phase6RunError(
            "phase6_depth60_v3_plan_invalid",
            "Depth-60 v3 plan identity/status/algorithm 无效。",
        )
    if plan.get("component_hash_algorithms") != {
        "source_bundle_sha256": "v2",
        "completion_telemetry_runtime_bundle_sha256": (
            "completion_telemetry_runtime_bundle_v1"
        ),
        "completion_telemetry_contract_bundle_sha256": (
            "completion_telemetry_contract_bundle_v1"
        ),
    }:
        raise Phase6RunError(
            "phase6_depth60_v3_plan_invalid",
            "Depth-60 v3 component hash algorithm 漂移。",
        )

    _validate_frozen_depth60_v2_lineage(root)
    if plan.get("supersedes") != {
        "plan_id": DEPTH60_SUCCESSOR_PLAN_ID,
        "plan_commitment_sha256": DEPTH60_V2_PLAN_COMMITMENT_SHA256,
        "source_bundle_sha256": DEPTH60_V2_SOURCE_BUNDLE_SHA256,
        "source_bundle_algorithm": "v2",
        "historical_plan_relative_path": DEPTH60_SUCCESSOR_PLAN_PATH.as_posix(),
        "historical_plan_bytes": DEPTH60_V2_PLAN_BYTES,
        "historical_plan_sha256": DEPTH60_V2_PLAN_FILE_SHA256,
        "historical_commitment_preserved": True,
        "historical_run_superseded": False,
    }:
        raise Phase6RunError(
            "phase6_depth60_v3_lineage_invalid",
            "Depth-60 v3 supersedes 血缘块不正确。",
        )
    if plan.get("authorization_boundary") != {
        "plan_alone_authorizes_online_run": False,
        "online_execution_authorized": False,
        "usable_as_runtime_binding": False,
        "supersedes_historical_online_authorization": False,
    }:
        raise Phase6RunError(
            "phase6_depth60_v3_plan_invalid",
            "Depth-60 v3 authorization boundary 漂移。",
        )
    if plan.get("claim_boundary") != {
        "model_quality_claim_allowed": False,
        "reproduces_historical_depth60_run": False,
        "historical_result_revalidated": False,
        "source_integrity_scope": (
            "current_tree_plus_completion_telemetry_runtime_and_contract_bundles"
        ),
    }:
        raise Phase6RunError(
            "phase6_depth60_v3_plan_invalid",
            "Depth-60 v3 claim boundary 漂移。",
        )

    try:
        actual_components = build_depth60_component_hashes_v3(root)
    except (OSError, ValueError) as exc:
        raise Phase6RunError(
            "phase6_depth60_v3_component_drift",
            "Depth-60 v3 telemetry component 无法安全重算。",
        ) from exc
    if plan.get("component_hashes") != actual_components:
        raise Phase6RunError(
            "phase6_depth60_v3_component_drift",
            "Depth-60 v3 component 与当前树不一致。",
        )
    commitment = plan.get("plan_commitment_sha256")
    computed = depth60_successor_v3_plan_commitment_sha256(plan)
    if (
        not isinstance(commitment, str)
        or _SHA256.fullmatch(commitment) is None
        or commitment != computed
    ):
        raise Phase6RunError(
            "phase6_depth60_v3_plan_invalid",
            "Depth-60 v3 plan commitment 无效。",
        )
    return {
        "status": "valid",
        "plan_id": DEPTH60_SUCCESSOR_V3_PLAN_ID,
        "plan_commitment_sha256": commitment,
        "source_bundle_algorithm": "v2",
        "supersedes_plan_id": DEPTH60_SUCCESSOR_PLAN_ID,
        "online_execution_authorized": False,
        "network_calls": 0,
        "model_calls": 0,
        "plan": plan,
    }


def _validate_frozen_depth60_v3_lineage(root: Path) -> dict[str, Any]:
    lexical = root / DEPTH60_SUCCESSOR_V3_PLAN_PATH
    path = lexical.resolve()
    if lexical.is_symlink() or not path.is_file() or not path.is_relative_to(root):
        raise Phase6RunError(
            "phase6_depth60_v3_lineage_invalid",
            "冻结的 Depth-60 v3 plan 缺失、逃逸或是 symlink。",
        )
    try:
        payload = path.read_bytes()
    except OSError as exc:
        raise Phase6RunError(
            "phase6_depth60_v3_lineage_invalid",
            "无法读取冻结的 Depth-60 v3 plan。",
        ) from exc
    if (
        len(payload) != DEPTH60_V3_PLAN_BYTES
        or hashlib.sha256(payload).hexdigest() != DEPTH60_V3_PLAN_FILE_SHA256
    ):
        raise Phase6RunError(
            "phase6_depth60_v3_lineage_invalid",
            "冻结的 Depth-60 v3 plan 字节 commitment 不匹配。",
        )
    plan = _load_json_object(path)
    _require_exact_fields(
        plan,
        {
            "schema_version",
            "plan_id",
            "status",
            "locked_at_utc",
            "evaluation_scope",
            "source_bundle_algorithm",
            "component_hash_algorithms",
            "component_hashes",
            "supersedes",
            "authorization_boundary",
            "claim_boundary",
            "plan_commitment_sha256",
        },
        "depth60 frozen v3 plan",
    )
    components = plan.get("component_hashes")
    if (
        plan.get("schema_version") != DEPTH60_SUCCESSOR_V3_PLAN_SCHEMA_VERSION
        or plan.get("plan_id") != DEPTH60_SUCCESSOR_V3_PLAN_ID
        or plan.get("status") != "locked_offline_not_run"
        or plan.get("evaluation_scope") != "source_integrity_commitment_only"
        or plan.get("source_bundle_algorithm") != "v2"
        or not isinstance(plan.get("locked_at_utc"), str)
        or not plan["locked_at_utc"].endswith("Z")
        or plan.get("component_hash_algorithms")
        != {
            "source_bundle_sha256": "v2",
            "completion_telemetry_runtime_bundle_sha256": (
                "completion_telemetry_runtime_bundle_v1"
            ),
            "completion_telemetry_contract_bundle_sha256": (
                "completion_telemetry_contract_bundle_v1"
            ),
        }
        or plan.get("plan_commitment_sha256")
        != DEPTH60_V3_PLAN_COMMITMENT_SHA256
        or depth60_successor_v3_plan_commitment_sha256(plan)
        != DEPTH60_V3_PLAN_COMMITMENT_SHA256
        or not isinstance(components, Mapping)
        or components.get("source_bundle_sha256")
        != DEPTH60_V3_SOURCE_BUNDLE_SHA256
        or components.get("completion_telemetry_runtime_bundle_sha256")
        != DEPTH60_V3_RUNTIME_BUNDLE_SHA256
        or components.get("completion_telemetry_contract_bundle_sha256")
        != DEPTH60_V3_CONTRACT_BUNDLE_SHA256
    ):
        raise Phase6RunError(
            "phase6_depth60_v3_lineage_invalid",
            "冻结的 Depth-60 v3 plan 全体字段 commitment 无效。",
        )
    _validate_frozen_depth60_v2_lineage(root)
    if plan.get("supersedes") != {
        "plan_id": DEPTH60_SUCCESSOR_PLAN_ID,
        "plan_commitment_sha256": DEPTH60_V2_PLAN_COMMITMENT_SHA256,
        "source_bundle_sha256": DEPTH60_V2_SOURCE_BUNDLE_SHA256,
        "source_bundle_algorithm": "v2",
        "historical_plan_relative_path": DEPTH60_SUCCESSOR_PLAN_PATH.as_posix(),
        "historical_plan_bytes": DEPTH60_V2_PLAN_BYTES,
        "historical_plan_sha256": DEPTH60_V2_PLAN_FILE_SHA256,
        "historical_commitment_preserved": True,
        "historical_run_superseded": False,
    } or plan.get("authorization_boundary") != {
        "plan_alone_authorizes_online_run": False,
        "online_execution_authorized": False,
        "usable_as_runtime_binding": False,
        "supersedes_historical_online_authorization": False,
    } or plan.get("claim_boundary") != {
        "model_quality_claim_allowed": False,
        "reproduces_historical_depth60_run": False,
        "historical_result_revalidated": False,
        "source_integrity_scope": (
            "current_tree_plus_completion_telemetry_runtime_and_contract_bundles"
        ),
    }:
        raise Phase6RunError(
            "phase6_depth60_v3_lineage_invalid",
            "冻结的 Depth-60 v3 lineage 或边界字段无效。",
        )
    return plan


def _validate_depth60_successor_v4_plan(
    root: Path, resolved_plan: Path
) -> dict[str, Any]:
    lexical = root / DEPTH60_SUCCESSOR_V4_PLAN_PATH
    if lexical.is_symlink() or resolved_plan != lexical.resolve():
        raise Phase6RunError(
            "phase6_depth60_v4_plan_invalid",
            "Depth-60 v4 plan 固定路径缺失、逃逸或是 symlink。",
        )
    plan = _load_json_object(resolved_plan)
    _require_exact_fields(
        plan,
        {
            "schema_version",
            "plan_id",
            "status",
            "locked_at_utc",
            "evaluation_scope",
            "source_bundle_algorithm",
            "component_hash_algorithms",
            "component_hashes",
            "supersedes",
            "authorization_boundary",
            "claim_boundary",
            "plan_commitment_sha256",
        },
        "depth60 v4 successor plan",
    )
    if (
        plan.get("schema_version") != DEPTH60_SUCCESSOR_V4_PLAN_SCHEMA_VERSION
        or plan.get("plan_id") != DEPTH60_SUCCESSOR_V4_PLAN_ID
        or plan.get("status") != "locked_offline_not_run"
        or plan.get("evaluation_scope") != "source_integrity_commitment_only"
        or plan.get("source_bundle_algorithm") != "v2"
        or not isinstance(plan.get("locked_at_utc"), str)
        or not plan["locked_at_utc"].endswith("Z")
    ):
        raise Phase6RunError(
            "phase6_depth60_v4_plan_invalid",
            "Depth-60 v4 plan identity/status/algorithm 无效。",
        )
    if plan.get("component_hash_algorithms") != {
        "source_bundle_sha256": "v2",
        "completion_telemetry_runtime_bundle_sha256": (
            "completion_telemetry_runtime_bundle_v1"
        ),
        "completion_telemetry_contract_bundle_sha256": (
            "completion_telemetry_contract_bundle_v1"
        ),
    }:
        raise Phase6RunError(
            "phase6_depth60_v4_plan_invalid",
            "Depth-60 v4 component hash algorithm 漂移。",
        )

    _validate_frozen_depth60_v3_lineage(root)
    if plan.get("supersedes") != {
        "plan_id": DEPTH60_SUCCESSOR_V3_PLAN_ID,
        "plan_commitment_sha256": DEPTH60_V3_PLAN_COMMITMENT_SHA256,
        "source_bundle_sha256": DEPTH60_V3_SOURCE_BUNDLE_SHA256,
        "completion_telemetry_runtime_bundle_sha256": (
            DEPTH60_V3_RUNTIME_BUNDLE_SHA256
        ),
        "completion_telemetry_contract_bundle_sha256": (
            DEPTH60_V3_CONTRACT_BUNDLE_SHA256
        ),
        "source_bundle_algorithm": "v2",
        "historical_plan_relative_path": DEPTH60_SUCCESSOR_V3_PLAN_PATH.as_posix(),
        "historical_plan_bytes": DEPTH60_V3_PLAN_BYTES,
        "historical_plan_sha256": DEPTH60_V3_PLAN_FILE_SHA256,
        "historical_commitment_preserved": True,
        "historical_run_superseded": False,
    }:
        raise Phase6RunError(
            "phase6_depth60_v4_lineage_invalid",
            "Depth-60 v4 supersedes 血缘块不正确。",
        )
    if plan.get("authorization_boundary") != {
        "plan_alone_authorizes_online_run": False,
        "online_execution_authorized": False,
        "usable_as_runtime_binding": False,
        "supersedes_historical_online_authorization": False,
    }:
        raise Phase6RunError(
            "phase6_depth60_v4_plan_invalid",
            "Depth-60 v4 authorization boundary 漂移。",
        )
    if plan.get("claim_boundary") != {
        "model_quality_claim_allowed": False,
        "reproduces_historical_depth60_run": False,
        "historical_result_revalidated": False,
        "source_integrity_scope": (
            "current_tree_plus_completion_telemetry_runtime_and_contract_bundles"
        ),
    }:
        raise Phase6RunError(
            "phase6_depth60_v4_plan_invalid",
            "Depth-60 v4 claim boundary 漂移。",
        )
    try:
        actual_components = build_depth60_component_hashes_v4(root)
    except (OSError, ValueError) as exc:
        raise Phase6RunError(
            "phase6_depth60_v4_component_drift",
            "Depth-60 v4 telemetry component 无法安全重算。",
        ) from exc
    if plan.get("component_hashes") != actual_components:
        raise Phase6RunError(
            "phase6_depth60_v4_component_drift",
            "Depth-60 v4 component 与当前树不一致。",
            not_run=True,
        )
    commitment = plan.get("plan_commitment_sha256")
    if (
        not isinstance(commitment, str)
        or _SHA256.fullmatch(commitment) is None
        or commitment != depth60_successor_v4_plan_commitment_sha256(plan)
    ):
        raise Phase6RunError(
            "phase6_depth60_v4_plan_invalid",
            "Depth-60 v4 plan commitment 无效。",
        )
    return {
        "status": "valid",
        "plan_id": DEPTH60_SUCCESSOR_V4_PLAN_ID,
        "plan_commitment_sha256": commitment,
        "source_bundle_algorithm": "v2",
        "supersedes_plan_id": DEPTH60_SUCCESSOR_V3_PLAN_ID,
        "online_execution_authorized": False,
        "network_calls": 0,
        "model_calls": 0,
        "plan": plan,
    }


def _validate_frozen_depth60_v4_lineage(root: Path) -> dict[str, Any]:
    lexical = root / DEPTH60_SUCCESSOR_V4_PLAN_PATH
    path = lexical.resolve()
    if lexical.is_symlink() or not path.is_file() or not path.is_relative_to(root):
        raise Phase6RunError(
            "phase6_depth60_v4_lineage_invalid",
            "冻结的 Depth-60 v4 plan 缺失、逃逸或是 symlink。",
        )
    try:
        payload = path.read_bytes()
    except OSError as exc:
        raise Phase6RunError(
            "phase6_depth60_v4_lineage_invalid",
            "无法读取冻结的 Depth-60 v4 plan。",
        ) from exc
    if (
        len(payload) != DEPTH60_V4_PLAN_BYTES
        or hashlib.sha256(payload).hexdigest() != DEPTH60_V4_PLAN_FILE_SHA256
    ):
        raise Phase6RunError(
            "phase6_depth60_v4_lineage_invalid",
            "冻结的 Depth-60 v4 plan 字节 commitment 不匹配。",
        )
    plan = _load_json_object(path)
    _require_exact_fields(
        plan,
        {
            "schema_version",
            "plan_id",
            "status",
            "locked_at_utc",
            "evaluation_scope",
            "source_bundle_algorithm",
            "component_hash_algorithms",
            "component_hashes",
            "supersedes",
            "authorization_boundary",
            "claim_boundary",
            "plan_commitment_sha256",
        },
        "depth60 frozen v4 plan",
    )
    components = plan.get("component_hashes")
    if (
        plan.get("schema_version") != DEPTH60_SUCCESSOR_V4_PLAN_SCHEMA_VERSION
        or plan.get("plan_id") != DEPTH60_SUCCESSOR_V4_PLAN_ID
        or plan.get("status") != "locked_offline_not_run"
        or plan.get("evaluation_scope") != "source_integrity_commitment_only"
        or plan.get("source_bundle_algorithm") != "v2"
        or not isinstance(plan.get("locked_at_utc"), str)
        or not plan["locked_at_utc"].endswith("Z")
        or plan.get("component_hash_algorithms")
        != {
            "source_bundle_sha256": "v2",
            "completion_telemetry_runtime_bundle_sha256": (
                "completion_telemetry_runtime_bundle_v1"
            ),
            "completion_telemetry_contract_bundle_sha256": (
                "completion_telemetry_contract_bundle_v1"
            ),
        }
        or plan.get("plan_commitment_sha256")
        != DEPTH60_V4_PLAN_COMMITMENT_SHA256
        or depth60_successor_v4_plan_commitment_sha256(plan)
        != DEPTH60_V4_PLAN_COMMITMENT_SHA256
        or not isinstance(components, Mapping)
        or components.get("source_bundle_sha256")
        != DEPTH60_V4_SOURCE_BUNDLE_SHA256
        or components.get("completion_telemetry_runtime_bundle_sha256")
        != DEPTH60_V4_RUNTIME_BUNDLE_SHA256
        or components.get("completion_telemetry_contract_bundle_sha256")
        != DEPTH60_V4_CONTRACT_BUNDLE_SHA256
    ):
        raise Phase6RunError(
            "phase6_depth60_v4_lineage_invalid",
            "冻结的 Depth-60 v4 plan 全体字段 commitment 无效。",
        )
    _validate_frozen_depth60_v3_lineage(root)
    if plan.get("supersedes") != {
        "plan_id": DEPTH60_SUCCESSOR_V3_PLAN_ID,
        "plan_commitment_sha256": DEPTH60_V3_PLAN_COMMITMENT_SHA256,
        "source_bundle_sha256": DEPTH60_V3_SOURCE_BUNDLE_SHA256,
        "completion_telemetry_runtime_bundle_sha256": (
            DEPTH60_V3_RUNTIME_BUNDLE_SHA256
        ),
        "completion_telemetry_contract_bundle_sha256": (
            DEPTH60_V3_CONTRACT_BUNDLE_SHA256
        ),
        "source_bundle_algorithm": "v2",
        "historical_plan_relative_path": DEPTH60_SUCCESSOR_V3_PLAN_PATH.as_posix(),
        "historical_plan_bytes": DEPTH60_V3_PLAN_BYTES,
        "historical_plan_sha256": DEPTH60_V3_PLAN_FILE_SHA256,
        "historical_commitment_preserved": True,
        "historical_run_superseded": False,
    } or plan.get("authorization_boundary") != {
        "plan_alone_authorizes_online_run": False,
        "online_execution_authorized": False,
        "usable_as_runtime_binding": False,
        "supersedes_historical_online_authorization": False,
    } or plan.get("claim_boundary") != {
        "model_quality_claim_allowed": False,
        "reproduces_historical_depth60_run": False,
        "historical_result_revalidated": False,
        "source_integrity_scope": (
            "current_tree_plus_completion_telemetry_runtime_and_contract_bundles"
        ),
    }:
        raise Phase6RunError(
            "phase6_depth60_v4_lineage_invalid",
            "冻结的 Depth-60 v4 lineage 或边界字段无效。",
        )
    return plan


def _validate_depth60_successor_v5_plan(
    root: Path, resolved_plan: Path
) -> dict[str, Any]:
    lexical = root / DEPTH60_SUCCESSOR_V5_PLAN_PATH
    if lexical.is_symlink() or resolved_plan != lexical.resolve():
        raise Phase6RunError(
            "phase6_depth60_v5_plan_invalid",
            "Depth-60 v5 plan 固定路径缺失、逃逸或是 symlink。",
        )
    plan = _load_json_object(resolved_plan)
    _require_exact_fields(
        plan,
        {
            "schema_version",
            "plan_id",
            "status",
            "locked_at_utc",
            "evaluation_scope",
            "source_bundle_algorithm",
            "component_hash_algorithms",
            "component_hashes",
            "supersedes",
            "authorization_boundary",
            "claim_boundary",
            "plan_commitment_sha256",
        },
        "depth60 v5 successor plan",
    )
    if (
        plan.get("schema_version") != DEPTH60_SUCCESSOR_V5_PLAN_SCHEMA_VERSION
        or plan.get("plan_id") != DEPTH60_SUCCESSOR_V5_PLAN_ID
        or plan.get("status") != "locked_offline_not_run"
        or plan.get("evaluation_scope") != "source_integrity_commitment_only"
        or plan.get("source_bundle_algorithm") != "v2"
        or not isinstance(plan.get("locked_at_utc"), str)
        or not plan["locked_at_utc"].endswith("Z")
        or plan.get("component_hash_algorithms")
        != {
            "source_bundle_sha256": "v2",
            "completion_telemetry_runtime_bundle_sha256": (
                "completion_telemetry_runtime_bundle_v1"
            ),
            "completion_telemetry_contract_bundle_sha256": (
                "completion_telemetry_contract_bundle_v1"
            ),
        }
    ):
        raise Phase6RunError(
            "phase6_depth60_v5_plan_invalid",
            "Depth-60 v5 plan identity/status/algorithm 无效。",
        )
    _validate_frozen_depth60_v4_lineage(root)
    if plan.get("supersedes") != {
        "plan_id": DEPTH60_SUCCESSOR_V4_PLAN_ID,
        "plan_commitment_sha256": DEPTH60_V4_PLAN_COMMITMENT_SHA256,
        "source_bundle_sha256": DEPTH60_V4_SOURCE_BUNDLE_SHA256,
        "completion_telemetry_runtime_bundle_sha256": (
            DEPTH60_V4_RUNTIME_BUNDLE_SHA256
        ),
        "completion_telemetry_contract_bundle_sha256": (
            DEPTH60_V4_CONTRACT_BUNDLE_SHA256
        ),
        "source_bundle_algorithm": "v2",
        "historical_plan_relative_path": DEPTH60_SUCCESSOR_V4_PLAN_PATH.as_posix(),
        "historical_plan_bytes": DEPTH60_V4_PLAN_BYTES,
        "historical_plan_sha256": DEPTH60_V4_PLAN_FILE_SHA256,
        "historical_commitment_preserved": True,
        "historical_run_superseded": False,
    }:
        raise Phase6RunError(
            "phase6_depth60_v5_lineage_invalid",
            "Depth-60 v5 supersedes 血缘块不正确。",
        )
    if plan.get("authorization_boundary") != {
        "plan_alone_authorizes_online_run": False,
        "online_execution_authorized": False,
        "usable_as_runtime_binding": False,
        "supersedes_historical_online_authorization": False,
    } or plan.get("claim_boundary") != {
        "model_quality_claim_allowed": False,
        "reproduces_historical_depth60_run": False,
        "historical_result_revalidated": False,
        "source_integrity_scope": (
            "enumerated_source_dependency_closure_plus_explicit_completion_telemetry_"
            "runtime_and_contract_bundles"
        ),
    }:
        raise Phase6RunError(
            "phase6_depth60_v5_plan_invalid",
            "Depth-60 v5 authorization 或 claim boundary 漂移。",
        )
    try:
        actual_components = build_depth60_component_hashes_v5(root)
    except (OSError, ValueError) as exc:
        raise Phase6RunError(
            "phase6_depth60_v5_component_drift",
            "Depth-60 v5 telemetry component 无法安全重算。",
        ) from exc
    if plan.get("component_hashes") != actual_components:
        raise Phase6RunError(
            "phase6_depth60_v5_component_drift",
            "Depth-60 v5 component 与当前树不一致。",
            not_run=True,
        )
    commitment = plan.get("plan_commitment_sha256")
    if (
        not isinstance(commitment, str)
        or _SHA256.fullmatch(commitment) is None
        or commitment != depth60_successor_v5_plan_commitment_sha256(plan)
    ):
        raise Phase6RunError(
            "phase6_depth60_v5_plan_invalid",
            "Depth-60 v5 plan commitment 无效。",
        )
    return {
        "status": "valid",
        "plan_id": DEPTH60_SUCCESSOR_V5_PLAN_ID,
        "plan_commitment_sha256": commitment,
        "source_bundle_algorithm": "v2",
        "supersedes_plan_id": DEPTH60_SUCCESSOR_V4_PLAN_ID,
        "online_execution_authorized": False,
        "network_calls": 0,
        "model_calls": 0,
        "plan": plan,
    }


async def run_phase6_depth60_online(
    *,
    project_root: str | Path,
    plan_path: str | Path,
    output_directory: str | Path,
    authorization_id: str,
    authorization_expires_at_utc: str,
    expected_plan_commitment: str,
    confirm_online: bool,
    environment: Mapping[str, str] | None = None,
    agent_runner: Any = None,
) -> dict[str, Any]:
    if not confirm_online:
        raise Phase6RunError(
            "phase6_online_confirmation_required",
            "Depth-60 在线运行需要 --confirm-online。",
            not_run=True,
        )
    validation = validate_phase6_depth60_plan(project_root, plan_path)
    if validation.get("plan_id") != DEPTH60_PLAN_ID:
        raise Phase6RunError(
            "phase6_depth60_successor_plan_not_executable",
            "Depth-60 后继 plan 只是源码完整性承诺，不能授权在线运行。",
            not_run=True,
        )
    if (
        not isinstance(expected_plan_commitment, str)
        or _SHA256.fullmatch(expected_plan_commitment) is None
        or expected_plan_commitment
        != validation["plan_commitment_sha256"]
    ):
        raise Phase6RunError(
            "phase6_depth60_expected_commitment_mismatch",
            "用户授权绑定的 expected plan commitment 与当前 plan 不一致。",
        )
    if not isinstance(authorization_id, str) or _AUTHORIZATION_ID.fullmatch(
        authorization_id
    ) is None:
        raise Phase6RunError(
            "phase6_depth60_authorization_id_invalid", "授权 ID 格式无效。"
        )
    expiry = _parse_future_utc(authorization_expires_at_utc)
    canonical_expiry = expiry.isoformat().replace("+00:00", "Z")
    root = Path(project_root).resolve()
    output = Path(output_directory).resolve()
    artifacts_root = (root / "artifacts").resolve()
    receipt_root = (artifacts_root / "phase6_deepseek_depth60").resolve()
    if output == artifacts_root or not output.is_relative_to(artifacts_root):
        raise Phase6RunError(
            "phase6_output_path_not_allowed",
            "Depth-60 输出必须是 artifacts 下的新子目录。",
        )
    if output == receipt_root or output.is_relative_to(receipt_root):
        raise Phase6RunError(
            "phase6_depth60_receipt_namespace_reserved",
            "Depth-60 输出不得占用 single-use receipt 命名空间。",
        )
    if output.exists():
        raise Phase6RunError(
            "phase6_output_directory_exists", "Depth-60 输出目录已存在。"
        )
    environment_source = os.environ if environment is None else environment
    required_window_seconds = float(validation["plan"]["budget"]["total_timeout_seconds"])
    if (expiry - datetime.now(timezone.utc)).total_seconds() < required_window_seconds:
        raise Phase6RunError(
            "phase6_depth60_authorization_window_too_short",
            "授权有效期必须覆盖冻结的 90 分钟总运行时限。",
        )
    dependency_environment = validate_eval_v2_dependency_environment(root)
    readiness = phase6_status(provider="deepseek", environment=environment_source)
    if readiness["online_run_status"] != "ready_requires_explicit_confirmation":
        raise Phase6RunError(
            str(readiness.get("not_run_reason") or "phase6_depth60_not_ready"),
            "DeepSeek Phase 6 runtime 未就绪；未消费授权。",
            not_run=True,
        )
    plan = validation["plan"]
    receipt_root.mkdir(parents=True, exist_ok=True)
    receipt_path = receipt_root / (
        validation["plan_commitment_sha256"] + ".receipt.json"
    )
    terminal_path = receipt_root / (
        validation["plan_commitment_sha256"] + ".terminal.json"
    )
    if terminal_path.exists():
        raise Phase6RunError(
            "phase6_depth60_plan_already_consumed",
            "该 Depth-60 plan 已存在 terminal receipt，禁止再次运行。",
        )
    receipt = {
        "schema_version": "phase6-depth60-receipt/1.0",
        "status": "authorization_consumed_before_provider_initialization",
        "plan_id": validation["plan_id"],
        "plan_commitment_sha256": validation["plan_commitment_sha256"],
        "expected_plan_commitment": expected_plan_commitment,
        "authorization_id": authorization_id,
        "authorization_expires_at_utc": canonical_expiry,
        "consumed_at_utc": datetime.now(timezone.utc).isoformat().replace(
            "+00:00", "Z"
        ),
        "output_relative_path": output.relative_to(root).as_posix(),
        "api_key_persisted": False,
        "retry_or_resume_authorized": False,
        "dependency_environment_status": dependency_environment["status"],
    }
    try:
        with receipt_path.open("x", encoding="utf-8", newline="\n") as handle:
            json.dump(
                receipt,
                handle,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
    except FileExistsError as exc:
        raise Phase6RunError(
            "phase6_depth60_plan_already_consumed",
            "该 Depth-60 plan commitment 已消费，禁止第二次运行。",
        ) from exc
    consume_receipt_sha256 = _sha256_file(receipt_path)
    runtime_binding = {
        "plan_id": validation["plan_id"],
        "plan_commitment_sha256": validation["plan_commitment_sha256"],
        "selected_task_ids": plan["selection"]["selected_task_ids"],
        "component_hashes": plan["component_hashes"],
        "authorization_id_sha256": hashlib.sha256(
            authorization_id.encode("utf-8")
        ).hexdigest(),
        "authorization_expires_at_utc": canonical_expiry,
        "consume_receipt_relative_path": receipt_path.relative_to(root).as_posix(),
        "consume_receipt_sha256": consume_receipt_sha256,
        "claim_boundary": plan["claim_boundary"],
    }
    try:
        result = await _run_phase6_online_evaluation_impl(
            project_root=root,
            tasks_path=root / "evals/phase6_agent_tasks.jsonl",
            split_manifest_path=root / "evals/phase6_splits.json",
            output_directory=output,
            provider=plan["provider"]["provider_id"],
            model=plan["provider"]["model_alias"],
            split=plan["provider"]["split"],
            max_cases=plan["provider"]["max_cases"],
            max_turns=plan["provider"]["max_turns"],
            case_timeout_seconds=plan["provider"]["case_timeout_seconds"],
            confirm_online=True,
            deepseek_pricing_snapshot_date=plan["budget"][
                "pricing_snapshot_date"
            ],
            deepseek_pricing_source_url=plan["budget"]["pricing_source_url"],
            local_observed_cost_stop_cny=plan["budget"][
                "local_observed_cost_stop_cny"
            ],
            total_input_tokens_cap=plan["budget"]["total_input_tokens_cap"],
            total_output_tokens_cap=plan["budget"]["total_output_tokens_cap"],
            total_requests_cap=plan["budget"]["total_requests_cap"],
            total_timeout_seconds=plan["budget"]["total_timeout_seconds"],
            authorization_deadline_utc=expiry,
            environment=environment_source,
            agent_runner=agent_runner,
            _depth60_plan_binding=runtime_binding,
        )
    except Exception as exc:
        # The receipt remains consumed on every outcome, including setup or
        # Provider failure, so an interrupted run cannot silently become a retry.
        terminal = {
            "schema_version": "phase6-depth60-terminal/1.0",
            "status": "failed",
            "plan_id": validation["plan_id"],
            "plan_commitment_sha256": validation["plan_commitment_sha256"],
            "consume_receipt_sha256": consume_receipt_sha256,
            "authorization_id_sha256": runtime_binding[
                "authorization_id_sha256"
            ],
            "completed_at_utc": datetime.now(timezone.utc).isoformat().replace(
                "+00:00", "Z"
            ),
            "error_code": _safe_terminal_error_code(exc),
            "output_relative_path": output.relative_to(root).as_posix(),
            "phase6_manifest_sha256": (
                _sha256_file(output / "phase6_manifest.json")
                if (output / "phase6_manifest.json").is_file()
                else None
            ),
            "phase6_report_sha256": (
                _sha256_file(output / "phase6_report.json")
                if (output / "phase6_report.json").is_file()
                else None
            ),
        }
        try:
            _write_exclusive_json(terminal_path, terminal)
        except Exception as terminal_exc:
            if hasattr(exc, "add_note"):
                exc.add_note(
                    "Depth-60 terminal receipt write failed: "
                    + type(terminal_exc).__name__
                )
        raise
    report = result["report"]
    terminal = {
        "schema_version": "phase6-depth60-terminal/1.0",
        "status": report["run_status"],
        "plan_id": validation["plan_id"],
        "plan_commitment_sha256": validation["plan_commitment_sha256"],
        "consume_receipt_sha256": consume_receipt_sha256,
        "authorization_id_sha256": runtime_binding["authorization_id_sha256"],
        "completed_at_utc": datetime.now(timezone.utc).isoformat().replace(
            "+00:00", "Z"
        ),
        "error_code": report.get("stop_reason"),
        "output_relative_path": output.relative_to(root).as_posix(),
        "attempted_case_count": report["attempted_case_count"],
        "completed_case_count": report["completed_case_count"],
        "not_started_case_count": report["not_started_case_count"],
        "phase6_manifest_sha256": _sha256_file(output / "phase6_manifest.json"),
        "phase6_report_sha256": _sha256_file(output / "phase6_report.json"),
    }
    _write_exclusive_json(terminal_path, terminal)
    result["depth60_plan"] = {
        "plan_id": validation["plan_id"],
        "plan_commitment_sha256": validation["plan_commitment_sha256"],
        "single_use_receipt": receipt_path.relative_to(root).as_posix(),
        "single_use_receipt_sha256": consume_receipt_sha256,
        "terminal_receipt": terminal_path.relative_to(root).as_posix(),
        "terminal_receipt_sha256": _sha256_file(terminal_path),
        "authorization_expired_during_run": datetime.now(timezone.utc) >= expiry,
    }
    return result


def _write_exclusive_json(path: Path, payload: Mapping[str, Any]) -> None:
    with path.open("x", encoding="utf-8", newline="\n") as handle:
        json.dump(
            payload,
            handle,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())


def _safe_terminal_error_code(exc: Exception) -> str:
    code = getattr(exc, "code", None)
    if isinstance(code, str) and re.fullmatch(r"[a-z0-9_]{1,96}", code):
        return code
    return "phase6_depth60_" + type(exc).__name__.lower()


def _parse_future_utc(value: str) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise Phase6RunError(
            "phase6_depth60_authorization_expiry_invalid",
            "授权到期时间必须是以 Z 结尾的 UTC 时间。",
        )
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise Phase6RunError(
            "phase6_depth60_authorization_expiry_invalid",
            "授权到期时间不是有效 UTC 时间。",
        ) from exc
    if parsed <= datetime.now(timezone.utc):
        raise Phase6RunError(
            "phase6_depth60_authorization_expired", "授权已经过期。"
        )
    return parsed


def _load_json_object(path: Path) -> dict[str, Any]:
    def reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        output: dict[str, Any] = {}
        for key, value in pairs:
            if key in output:
                raise Phase6RunError(
                    "phase6_depth60_plan_invalid", "Plan 含重复 JSON key。"
                )
            output[key] = value
        return output

    try:
        raw = path.read_text(encoding="utf-8")
        value = json.loads(
            raw,
            object_pairs_hook=reject_duplicates,
            parse_constant=lambda item: (_ for _ in ()).throw(
                ValueError(f"non-finite value: {item}")
            ),
        )
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise Phase6RunError(
            "phase6_depth60_plan_invalid", "无法严格读取 Depth-60 plan。"
        ) from exc
    if not isinstance(value, dict):
        raise Phase6RunError(
            "phase6_depth60_plan_invalid", "Depth-60 plan 必须是 JSON object。"
        )
    return value


def _require_exact_fields(
    value: Mapping[str, Any], expected: set[str], label: str
) -> None:
    if set(value) != expected:
        raise Phase6RunError(
            "phase6_depth60_plan_invalid", f"{label} fields 不精确。"
        )


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
