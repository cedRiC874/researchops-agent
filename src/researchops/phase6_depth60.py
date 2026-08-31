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
from .phase6_source_bundle import phase6_depth60_source_bundle_sha256


DEPTH60_PLAN_ID = "phase6-deepseek-depth60-v1"
DEPTH60_PLAN_SCHEMA_VERSION = "1.0"
DEPTH60_PLAN_DOMAIN = b"researchops-phase6-deepseek-depth60-plan-v1\0"
DEPTH60_PLAN_PATH = Path("evals/phase6_deepseek_depth60_plan.json")
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


def build_depth60_component_hashes(project_root: str | Path) -> dict[str, str]:
    root = Path(project_root).resolve()
    return {
        "source_bundle_sha256": phase6_depth60_source_bundle_sha256(root),
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


def validate_phase6_depth60_plan(
    project_root: str | Path,
    plan_path: str | Path = DEPTH60_PLAN_PATH,
) -> dict[str, Any]:
    root = Path(project_root).resolve()
    resolved_plan = Path(plan_path)
    if not resolved_plan.is_absolute():
        resolved_plan = root / resolved_plan
    resolved_plan = resolved_plan.resolve()
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
