from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import math
import os
import platform
import re
import shutil
import tempfile
import time
from collections import Counter
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_UP
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .artifact_security import ArtifactPermissionError, enable_parent_acl_inheritance
from .audit import AuditLedger, safe_audit_value, sha256_json
from .model_providers import (
    ANTHROPIC_GENERIC_ONLINE_DISABLED_CODE,
    ProviderAdapter,
    get_provider,
    provider_transport_status,
)
from .phase6_agent import (
    PHASE6_MAX_OUTPUT_TOKENS,
    AgentRunRecord,
    ControlledExecutorBackend,
    LogicalAgentRequest,
    Phase6AgentError,
    phase6_sdk_status,
    run_phase6_agent,
)
from .phase6_eval import (
    PHASE6_SCHEMA_VERSION,
    Phase6ContractError,
    Phase6Task,
    Phase6TaskScore,
    aggregate_phase6_scores,
    load_phase6_tasks,
    phase6_failed_run,
    score_phase6_run,
)
from .phase6_source_bundle import phase6_depth60_source_bundle_sha256
from .tool_runtime import ControlledToolExecutor, build_project_tool_registry


PHASE6_RUNNER_VERSION = "1.9.0"
PHASE6_EVALUATION_MODE = "online_agents_sdk"
PHASE6_SUBJECT_UNDER_TEST = "agent_planning_tool_trace_and_final_answer"
_SPLITS = {"development", "holdout"}
_SAFE_MODEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_SHA256_HEX = re.compile(r"^[a-f0-9]{64}$")
_PATH_TRAVERSAL = re.compile(r"(?:\.\.[/\\])+[^\s\"'<>]*")
_WINDOWS_ABSOLUTE_PATH = re.compile(
    r"(?<!\w)(?:"
    r"[A-Za-z]:[\\/](?:[A-Za-z0-9._-]+[\\/])*[A-Za-z0-9._-]+|"
    r"\\\\[A-Za-z0-9._-]+\\(?:[A-Za-z0-9._-]+\\)*[A-Za-z0-9._-]+|"
    r"//(?:[A-Za-z0-9._-]+/)*[A-Za-z0-9._-]+"
    r")"
)
_UNIX_ABSOLUTE_PATH = re.compile(
    # ``\w`` is Unicode-aware, so a slash inside terms such as
    # ``k-匿名/l-多样性`` is not mistaken for the start of a path.  Blocking a
    # preceding slash also avoids treating the second slash in ``https://`` as
    # an absolute path.  Start-of-string, whitespace, quotes, brackets and
    # assignment punctuation still satisfy this boundary.
    r"(?<![\w/])/(?:[A-Za-z0-9._-]+/)*[A-Za-z0-9._-]+"
)

# DeepSeek V4 Flash peak pricing is deliberately fixed rather than accepted as
# operator-controlled rates. Every reported input token is treated as a cache
# miss. The snapshot date and official URL remain operator-supplied provenance
# because this runner performs no network lookup.
_DEEPSEEK_INPUT_CACHE_MISS_CNY_PER_MILLION = Decimal("3")
_DEEPSEEK_OUTPUT_CNY_PER_MILLION = Decimal("9")
_DEEPSEEK_CASE_INPUT_TOKEN_RESERVE = 250_000
_DEEPSEEK_CASE_OUTPUT_TOKEN_RESERVE = 16_000
_DEEPSEEK_CASE_COST_RESERVE_CNY = Decimal("4")
_MONEY_QUANTUM = Decimal("0.000001")
_CATEGORY_TAGS = frozenset({"typical", "edge", "adversarial"})
_DEPTH60_PLAN_DOMAIN = b"researchops-phase6-deepseek-depth60-plan-v1\0"
_DEPTH60_PLAN_RELATIVE_PATH = Path("evals/phase6_deepseek_depth60_plan.json")
_DEPTH60_RECEIPT_ROOT = Path("artifacts/phase6_deepseek_depth60")
_DEPTH60_INPUT_COMPONENT_PATHS = {
    "synthetic_trial_csv_sha256": Path("data/synthetic_trial.csv"),
    "synthetic_trial_design_sha256": Path("data/synthetic_trial_design.json"),
    "phase3_analysis_bundle_sha256": Path("artifacts/phase3/analysis_bundle.json"),
    "phase3_effect_estimates_png_sha256": Path(
        "artifacts/phase3/effect_estimates.png"
    ),
}

AgentRunner = Callable[..., Awaitable[AgentRunRecord] | AgentRunRecord]


@dataclass(frozen=True)
class _DeepSeekCnyPolicy:
    pricing_snapshot_date: str
    pricing_source_url: str
    local_observed_cost_stop_cny: Decimal
    total_input_tokens_cap: int
    total_output_tokens_cap: int
    total_requests_cap: int
    total_timeout_seconds: float


class Phase6RunError(RuntimeError):
    """A stable Phase 6 harness error that never contains an API response body."""

    def __init__(self, code: str, message: str, *, not_run: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.not_run = not_run

    def to_dict(self) -> dict[str, str]:
        return {
            "status": "not_run" if self.not_run else "error",
            "error_code": self.code,
            "message": str(self),
        }


def phase6_status(
    *,
    provider: str = "openai",
    environment: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Report local readiness without importing a Runner or making a network call."""

    adapter = get_provider(provider)
    environment_source = os.environ if environment is None else environment
    configured_value = environment_source.get(adapter.api_key_env)
    configured = isinstance(configured_value, str) and bool(configured_value.strip())
    sdk = phase6_sdk_status()
    transport_status = provider_transport_status(adapter)
    sdk.update(
        {
            "api_key_configured": configured,
            "api_key_environment_variable": adapter.api_key_env,
            "provider": adapter.provider_id,
            "transport": adapter.transport_id,
            "provider_transport": transport_status,
            "provider_transport_ready": (
                transport_status["installed"] and transport_status["compatible"]
            ),
        }
    )
    if not sdk["installed"]:
        online_status = "not_run"
        reason = "sdk_not_installed"
    elif not transport_status["installed"]:
        online_status = "not_run"
        reason = "provider_transport_not_installed"
    elif not transport_status["compatible"]:
        online_status = "not_run"
        reason = "provider_transport_dependency_drift"
    elif not configured:
        online_status = "not_run"
        reason = "api_key_missing"
    elif adapter.provider_id == "anthropic":
        online_status = "not_run"
        reason = ANTHROPIC_GENERIC_ONLINE_DISABLED_CODE
    else:
        online_status = "ready_requires_explicit_confirmation"
        reason = None
    return {
        "status": "ok",
        "evaluation_mode": PHASE6_EVALUATION_MODE,
        "provider": adapter.provider_id,
        "transport": adapter.transport_id,
        "online_run_status": online_status,
        "not_run_reason": reason,
        "sdk": sdk,
        "network_calls": 0,
    }


def validate_phase6_suite(
    tasks_path: str | Path,
    split_manifest_path: str | Path,
) -> dict[str, Any]:
    """Strictly validate the closed corpus and its development/holdout manifest."""

    task_source = Path(tasks_path).resolve()
    split_source = Path(split_manifest_path).resolve()
    tasks = load_phase6_tasks(task_source, split_source)
    return {
        "status": "valid",
        "schema_version": "1.0",
        "task_schema_version": PHASE6_SCHEMA_VERSION,
        "task_count": len(tasks),
        "split_counts": dict(sorted(Counter(task.split for task in tasks).items())),
        "task_sha256": _sha256_file(task_source),
        "split_manifest_sha256": _sha256_file(split_source),
        "golden_isolation": "system under test is constructed only from Phase6Task.public_input",
        "network_calls": 0,
    }


async def run_phase6_online_evaluation(
    *,
    project_root: str | Path,
    tasks_path: str | Path,
    split_manifest_path: str | Path,
    output_directory: str | Path,
    provider: str = "openai",
    model: str,
    split: str,
    max_cases: int,
    max_turns: int = 8,
    case_timeout_seconds: float = 120.0,
    confirm_online: bool = False,
    input_price_per_million_usd: float | None = None,
    output_price_per_million_usd: float | None = None,
    deepseek_pricing_snapshot_date: str | None = None,
    deepseek_pricing_source_url: str | None = None,
    local_observed_cost_stop_cny: Decimal | str | float | None = None,
    total_input_tokens_cap: int | None = None,
    total_output_tokens_cap: int | None = None,
    total_requests_cap: int | None = None,
    total_timeout_seconds: float | None = None,
    authorization_deadline_utc: datetime | None = None,
    environment: Mapping[str, str] | None = None,
    agent_runner: AgentRunner | None = None,
) -> dict[str, Any]:
    """Public Phase 6 entrypoint; Depth-60 extensions are always denied here."""

    return await _run_phase6_online_evaluation_impl(
        project_root=project_root,
        tasks_path=tasks_path,
        split_manifest_path=split_manifest_path,
        output_directory=output_directory,
        provider=provider,
        model=model,
        split=split,
        max_cases=max_cases,
        max_turns=max_turns,
        case_timeout_seconds=case_timeout_seconds,
        confirm_online=confirm_online,
        input_price_per_million_usd=input_price_per_million_usd,
        output_price_per_million_usd=output_price_per_million_usd,
        deepseek_pricing_snapshot_date=deepseek_pricing_snapshot_date,
        deepseek_pricing_source_url=deepseek_pricing_source_url,
        local_observed_cost_stop_cny=local_observed_cost_stop_cny,
        total_input_tokens_cap=total_input_tokens_cap,
        total_output_tokens_cap=total_output_tokens_cap,
        total_requests_cap=total_requests_cap,
        total_timeout_seconds=total_timeout_seconds,
        authorization_deadline_utc=authorization_deadline_utc,
        environment=environment,
        agent_runner=agent_runner,
        _depth60_plan_binding=None,
    )


async def _run_phase6_online_evaluation_impl(
    *,
    project_root: str | Path,
    tasks_path: str | Path,
    split_manifest_path: str | Path,
    output_directory: str | Path,
    provider: str = "openai",
    model: str,
    split: str,
    max_cases: int,
    max_turns: int = 8,
    case_timeout_seconds: float = 120.0,
    confirm_online: bool = False,
    input_price_per_million_usd: float | None = None,
    output_price_per_million_usd: float | None = None,
    deepseek_pricing_snapshot_date: str | None = None,
    deepseek_pricing_source_url: str | None = None,
    local_observed_cost_stop_cny: Decimal | str | float | None = None,
    total_input_tokens_cap: int | None = None,
    total_output_tokens_cap: int | None = None,
    total_requests_cap: int | None = None,
    total_timeout_seconds: float | None = None,
    authorization_deadline_utc: datetime | None = None,
    environment: Mapping[str, str] | None = None,
    agent_runner: AgentRunner | None = None,
    _depth60_plan_binding: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Run selected cases sequentially against the real SDK adapter.

    ``agent_runner`` is injectable only to test this harness without networking.  It
    receives a ``LogicalAgentRequest`` built exclusively from ``task.public_input()``;
    evaluator goldens remain on the scoring side of the boundary.
    """

    _require_online_confirmation(confirm_online)
    adapter = get_provider(provider)
    if adapter.provider_id == "anthropic":
        raise Phase6RunError(
            ANTHROPIC_GENERIC_ONLINE_DISABLED_CODE,
            "Generic Phase 6 Anthropic 入口未获受控 pilot 授权；Models preflight receipt 不授权运行。",
            not_run=True,
        )
    normalized_model = adapter.validate_model(_validate_model(model))
    normalized_split = _validate_split(split)
    _require_positive_int("max_cases", max_cases)
    _require_positive_int("max_turns", max_turns)
    timeout_seconds = _require_positive_number(
        "case_timeout_seconds", case_timeout_seconds
    )
    prices = _validate_price_pair(
        input_price_per_million_usd, output_price_per_million_usd
    )
    deepseek_policy = _validate_deepseek_cny_policy(
        provider=adapter.provider_id,
        pricing_snapshot_date=deepseek_pricing_snapshot_date,
        pricing_source_url=deepseek_pricing_source_url,
        local_observed_cost_stop_cny=local_observed_cost_stop_cny,
        total_input_tokens_cap=total_input_tokens_cap,
        total_output_tokens_cap=total_output_tokens_cap,
        total_requests_cap=total_requests_cap,
        total_timeout_seconds=total_timeout_seconds,
        max_turns=max_turns,
    )
    authorization_deadline = _validate_authorization_deadline(
        authorization_deadline_utc,
        deepseek_policy=deepseek_policy,
    )
    if adapter.provider_id != "openai" and prices is not None:
        raise Phase6RunError(
            "phase6_pricing_unsupported_for_provider",
            "当前 provider 不支持旧式输入/输出两档价格；成本必须保持 unavailable。",
        )

    # All checks above are mutation-free. In particular, missing confirmation/key
    # returns before output directories, databases, Agents, or Runners are created.
    root = Path(project_root).resolve()
    output_path = _validate_output_directory(root, output_directory)
    task_source = Path(tasks_path).resolve()
    split_source = Path(split_manifest_path).resolve()
    tasks = load_phase6_tasks(task_source, split_source)
    selected = tuple(
        task for task in tasks if task.split == normalized_split
    )[:max_cases]
    if not selected:
        raise Phase6RunError("phase6_split_empty", "所选评测 split 没有任务。")
    if normalized_split == "holdout":
        raise Phase6RunError(
            "phase6_repo_local_holdout_rerun_forbidden",
            "冻结的 4 题 repo-local holdout 禁止再次在线运行。",
        )
    depth60_binding = _validate_depth60_runtime_binding(
        root=root,
        adapter=adapter,
        normalized_model=normalized_model,
        max_turns=max_turns,
        timeout_seconds=timeout_seconds,
        deepseek_policy=deepseek_policy,
        selected=selected,
        task_source=task_source,
        split_source=split_source,
        output_path=output_path,
        authorization_deadline=authorization_deadline,
        binding=_depth60_plan_binding,
    )

    sdk = phase6_sdk_status()
    if agent_runner is None and not sdk["installed"]:
        raise Phase6RunError(
            "sdk_not_installed",
            "未安装 OpenAI Agents SDK；在线评测未运行。",
            not_run=True,
        )
    # Read the provider credential only after all local confirmation, model,
    # budget, path and frozen-corpus checks have passed.
    api_key = _environment_api_key(adapter, environment)
    runner_impl = agent_runner or run_phase6_agent

    output_path.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(
        tempfile.mkdtemp(prefix=".researchops-phase6-", dir=output_path.parent)
    ).resolve()
    if not staging.is_relative_to(output_path.parent):
        raise Phase6RunError("phase6_staging_path_invalid", "临时评测目录越界。")

    campaign_started_ns = time.perf_counter_ns()
    deepseek_budget_state = _new_deepseek_budget_state(deepseek_policy)
    try:
        tool_source_root = _prepare_depth60_input_snapshot(
            root=root,
            staging=staging,
            binding=depth60_binding,
        )
        audit_path = staging / "phase6_audit.sqlite3"
        ledger = AuditLedger(audit_path)
        result_rows: list[dict[str, Any]] = []
        scores: list[Phase6TaskScore] = []
        audit_index: list[dict[str, Any]] = []
        harness_error_count = 0
        execution_failure_count = 0
        local_tool_attempt_count = 0
        local_tool_attempt_failure_count = 0
        stop_reason: str | None = None

        for task in selected:
            if deepseek_policy is not None:
                stop_reason = _deepseek_pre_case_guard(
                    policy=deepseek_policy,
                    state=deepseek_budget_state,
                    max_turns=max_turns,
                    elapsed_seconds=_elapsed_seconds(campaign_started_ns),
                    authorization_deadline_utc=authorization_deadline,
                )
                if stop_reason is not None:
                    break
                _mark_deepseek_case_attempted(deepseek_budget_state)
            public_input = task.public_input()
            request = _request_from_public_input(public_input)
            run_id = "RUN-PHASE6-" + task.task_id
            ledger.start_run(
                mode="phase6_online_agent_eval",
                run_id=run_id,
                request_summary={
                    "task_id": task.task_id,
                    "split": task.split,
                    "public_input_sha256": sha256_json(public_input),
                    "provider": adapter.provider_id,
                    "transport": adapter.transport_id,
                    "model": normalized_model,
                },
            )
            executor = ControlledToolExecutor(
                ledger,
                build_project_tool_registry(
                    root,
                    source_snapshot_root=tool_source_root,
                ),
            )
            backend = ControlledExecutorBackend(executor=executor, run_id=run_id)
            release_target = _release_target(root, request.release_name)
            release_existed_before = bool(
                release_target is not None and release_target.exists()
            )
            started_at_utc = datetime.now(timezone.utc).isoformat()
            started_ns = time.perf_counter_ns()
            deepseek_usage_observed = False
            deepseek_case_budget: dict[str, Any] | None = None
            harness_failure_this_case = False
            try:
                if release_existed_before:
                    raise Phase6RunError(
                        "phase6_release_target_exists",
                        "评测发布目标在运行前已存在，无法验证零副作用。",
                    )
                ledger.append_event(
                    run_id,
                    "agent_request_dispatched",
                    {
                        "task_id": task.task_id,
                        "provider": adapter.provider_id,
                        "transport": adapter.transport_id,
                        "model": normalized_model,
                        "max_turns": max_turns,
                        "tracing_disabled": True,
                        "public_input_sha256": sha256_json(public_input),
                    },
                    actor_kind="eval_harness",
                )
                record = await _run_one_agent(
                    runner_impl,
                    request=request,
                    backend=backend,
                    api_key=api_key,
                    provider=adapter,
                    model=normalized_model,
                    max_turns=max_turns,
                    timeout_seconds=_effective_case_timeout(
                        case_timeout_seconds=timeout_seconds,
                        policy=deepseek_policy,
                        campaign_started_ns=campaign_started_ns,
                        authorization_deadline_utc=authorization_deadline,
                    ),
                )
                if not isinstance(record, AgentRunRecord):
                    raise Phase6RunError(
                        "phase6_agent_record_invalid",
                        "Agent runner 必须返回 AgentRunRecord。",
                    )
                if deepseek_policy is not None:
                    deepseek_case_budget = _apply_deepseek_case_usage(
                        deepseek_budget_state, record
                    )
                    deepseek_usage_observed = True
                if record.model != normalized_model:
                    raise Phase6RunError(
                        "phase6_model_trace_mismatch",
                        "Agent 记录的模型与显式 --model 不一致。",
                    )
                if record.provider != adapter.provider_id:
                    raise Phase6RunError(
                        "phase6_provider_trace_mismatch",
                        "Agent 记录的 provider 与显式 --provider 不一致。",
                    )
                if record.transport != adapter.transport_id:
                    raise Phase6RunError(
                        "phase6_transport_trace_mismatch",
                        "Agent 记录的 transport 与 provider 配置不一致。",
                    )
                if not record.tracing_disabled:
                    raise Phase6RunError(
                        "phase6_external_tracing_enabled",
                        "在线评测要求关闭外部 tracing。",
                    )
                record = replace(record, cost_usd=_estimate_cost(record, prices))
                _record_model_usage(
                    ledger,
                    run_id,
                    record,
                    started_at_utc=started_at_utc,
                    provider=adapter.provider_id,
                    transport=adapter.transport_id,
                    prices=prices,
                )
                safe_record = _safe_record(record)
                ledger.append_event(
                    run_id,
                    "agent_trace_recorded",
                    {
                        "trace_sha256": sha256_json(record.to_dict()),
                        "status": record.status,
                        "provider": record.provider,
                        "transport": record.transport,
                        "tool_calls": safe_record["tool_calls"],
                        "approval_interruptions": safe_record[
                            "approval_interruptions"
                        ],
                        "final_output_sha256": _optional_text_sha256(
                            record.final_output
                        ),
                    },
                    actor_kind="eval_harness",
                )
                local_export = ledger.export_run(run_id)
                evidence_by_call_id, evidence_trace_valid = _evidence_from_sdk_trace(
                    record, local_export
                )
                (
                    safety_violation,
                    observed_side_effect_bypass,
                    safety_checks,
                ) = _observe_safety(
                    record=record,
                    local_export=local_export,
                    release_target=release_target,
                    release_existed_before=release_existed_before,
                    evidence_trace_valid=evidence_trace_valid,
                )
                score = score_phase6_run(
                    task,
                    record,
                    safety_violation=safety_violation,
                    observed_side_effect_bypass=observed_side_effect_bypass,
                )
                scores.append(score)
                ledger.append_event(
                    run_id,
                    "agent_run_scored",
                    {
                        "task_pass": score.task_pass,
                        "failure_reasons": list(score.failure_reasons),
                        "safety_violation": safety_violation,
                    },
                    actor_kind="eval_harness",
                )
                ledger.set_run_status(
                    run_id,
                    "waiting_approval"
                    if record.status == "waiting_approval"
                    else "completed",
                )
                result_rows.append(
                    {
                        "schema_version": "1.1",
                        "task_id": task.task_id,
                        "split": task.split,
                        "provider": adapter.provider_id,
                        "transport": adapter.transport_id,
                        "execution_status": "executed",
                        "tags": list(task.tags),
                        "category": _task_category(task),
                        "observation": safe_record,
                        "deepseek_cny_budget_observation": deepseek_case_budget,
                        "evidence_ids_by_tool_call": evidence_by_call_id,
                        "safety_checks": safety_checks,
                        "score": score.to_dict(),
                    }
                )
            except Exception as exc:
                if deepseek_policy is not None and not deepseek_usage_observed:
                    _mark_deepseek_usage_unavailable(deepseek_budget_state)
                if _is_execution_failure(exc):
                    execution_failure_count += 1
                else:
                    harness_error_count += 1
                    harness_failure_this_case = True
                latency_ms = max(
                    (time.perf_counter_ns() - started_ns) / 1_000_000, 0.0
                )
                error_code = _safe_error_code(exc)
                ledger.append_event(
                    run_id,
                    "agent_run_failed",
                    {
                        "error_code": error_code,
                        "error_class": type(exc).__name__,
                        "latency_ms": latency_ms,
                        "provider": adapter.provider_id,
                        "transport": adapter.transport_id,
                    },
                    actor_kind="eval_harness",
                )
                current_status = str(ledger.get_run(run_id)["status"])
                if current_status in {"running", "waiting_approval"}:
                    ledger.set_run_status(
                        run_id, "failed", terminal_error_code=error_code
                    )
                score = phase6_failed_run(
                    task, error_code, latency_ms=latency_ms
                )
                scores.append(score)
                result_rows.append(
                    {
                        "schema_version": "1.1",
                        "task_id": task.task_id,
                        "split": task.split,
                        "provider": adapter.provider_id,
                        "transport": adapter.transport_id,
                        "execution_status": "runner_error",
                        "error_code": error_code,
                        "tags": list(task.tags),
                        "category": _task_category(task),
                        "observation": None,
                        "deepseek_cny_budget_observation": deepseek_case_budget,
                        "evidence_ids_by_tool_call": {},
                        "safety_checks": None,
                        "score": score.to_dict(),
                    }
                )

            post_run_export = ledger.export_run(run_id)
            attempts = list(post_run_export.get("tool_attempts", ()))
            local_tool_attempt_count += len(attempts)
            local_tool_attempt_failure_count += sum(
                item.get("outcome") != "succeeded" for item in attempts
            )
            verification = ledger.verify_chain(run_id)
            audit_index.append(
                {
                    "task_id": task.task_id,
                    "run_id": run_id,
                    "provider": adapter.provider_id,
                    "transport": adapter.transport_id,
                    "chain_verification": verification.to_dict(),
                }
            )
            if not verification.valid:
                harness_error_count += 1
                harness_failure_this_case = True
                stop_reason = "deepseek_audit_chain_invalid"
            elif harness_failure_this_case and deepseek_policy is not None:
                stop_reason = "deepseek_harness_integrity_failure"
            if stop_reason is not None:
                break
            if deepseek_policy is not None:
                stop_reason = _deepseek_post_case_guard(
                    policy=deepseek_policy,
                    state=deepseek_budget_state,
                    max_turns=max_turns,
                    elapsed_seconds=_elapsed_seconds(campaign_started_ns),
                    case_budget=deepseek_case_budget,
                    authorization_deadline_utc=authorization_deadline,
                )
                if stop_reason is not None:
                    break

        attempted_task_ids = [row["task_id"] for row in result_rows]
        completed_task_ids = [
            row["task_id"]
            for row in result_rows
            if row["execution_status"] == "executed"
        ]
        attempted_task_id_set = set(attempted_task_ids)
        not_started_task_ids = [
            task.task_id for task in selected if task.task_id not in attempted_task_id_set
        ]
        report = aggregate_phase6_scores(
            scores, expected_task_ids=attempted_task_ids
        )
        report["task_count"] = len(selected)
        report["excluded_not_run"] = len(not_started_task_ids)
        report["not_run"] = [
            {"task_id": task_id, "reason": stop_reason or "not_started"}
            for task_id in not_started_task_ids
        ]
        report["selection"].update(
            {
                "planned_task_ids": [task.task_id for task in selected],
                "planned_task_count": len(selected),
                "attempted_task_ids": attempted_task_ids,
                "attempted_task_count": len(attempted_task_ids),
                "completed_task_ids": completed_task_ids,
                "completed_task_count": len(completed_task_ids),
                "not_started_task_ids": not_started_task_ids,
                "not_started_task_count": len(not_started_task_ids),
                "execution_coverage": (
                    len(attempted_task_ids) / len(selected) if selected else None
                ),
            }
        )
        failed_attempted = len(attempted_task_ids) - int(report["passed"])
        report["failure_denominators"] = {
            "attempted_cases": len(attempted_task_ids),
            "failed_attempted_cases": failed_attempted,
            "attempted_case_failure_rate": (
                failed_attempted / len(attempted_task_ids)
                if attempted_task_ids
                else None
            ),
            "planned_cases": len(selected),
            "not_started_cases": len(not_started_task_ids),
            "planned_case_nonpass_count": len(selected) - int(report["passed"]),
            "planned_case_nonpass_rate": (
                (len(selected) - int(report["passed"])) / len(selected)
                if selected
                else None
            ),
        }
        report["group_breakdowns"] = _grouped_execution_report(
            selected=selected,
            result_rows=result_rows,
            scores=scores,
        )
        tool_units = sum(
            len((row.get("observation") or {}).get("tool_calls", ()))
            for row in result_rows
        )
        tool_errors = sum(
            call.get("status") not in {"succeeded", "awaiting_approval"}
            for row in result_rows
            for call in (row.get("observation") or {}).get("tool_calls", ())
        )
        report.update(
            {
                "schema_version": "1.1",
                "evaluation_mode": PHASE6_EVALUATION_MODE,
                "subject_under_test": PHASE6_SUBJECT_UNDER_TEST,
                "provider": adapter.provider_id,
                "transport": adapter.transport_id,
                "model": normalized_model,
                "max_output_tokens": PHASE6_MAX_OUTPUT_TOKENS,
                "selected_split": normalized_split,
                "selected_case_count": len(selected),
                "attempted_case_count": len(attempted_task_ids),
                "completed_case_count": len(completed_task_ids),
                "not_started_case_count": len(not_started_task_ids),
                "run_status": "stopped" if stop_reason is not None else "completed",
                "stop_reason": stop_reason,
                "campaign_elapsed_seconds": _elapsed_seconds(campaign_started_ns),
                "authorization_deadline_utc": (
                    authorization_deadline.isoformat().replace("+00:00", "Z")
                    if authorization_deadline is not None
                    else None
                ),
                "depth60_plan_binding": depth60_binding,
                "tool_input_snapshot": _depth60_input_snapshot_report(
                    depth60_binding
                ),
                "harness_error_count": harness_error_count,
                "execution_failure_count": execution_failure_count,
                "observed_tool_error_rate": (
                    tool_errors / tool_units if tool_units else None
                ),
                "observed_tool_error_units": tool_units,
                "observed_tool_errors": tool_errors,
                "local_tool_attempt_count": local_tool_attempt_count,
                "local_tool_attempt_failure_count": local_tool_attempt_failure_count,
                "local_tool_attempt_error_rate": (
                    local_tool_attempt_failure_count / local_tool_attempt_count
                    if local_tool_attempt_count
                    else None
                ),
                "sdk_tool_call_failure_rate": (
                    tool_errors / tool_units if tool_units else None
                ),
                "pricing": _pricing_manifest(
                    prices,
                    provider=adapter.provider_id,
                    deepseek_policy=deepseek_policy,
                ),
                "deepseek_cny_policy": _deepseek_cny_policy_report(
                    deepseek_policy,
                    deepseek_budget_state,
                    attempted_case_count=len(attempted_task_ids),
                ),
            }
        )
        _apply_deepseek_cost_report(
            report,
            policy=deepseek_policy,
            state=deepseek_budget_state,
            attempted_case_count=len(attempted_task_ids),
        )

        if depth60_binding is not None:
            _remove_depth60_input_snapshot(
                staging=staging,
                snapshot_root=tool_source_root,
            )

        _write_jsonl(staging / "phase6_results.jsonl", result_rows)
        _write_json(staging / "phase6_report.json", report)
        _write_json(
            staging / "phase6_audit_index.json",
            {"schema_version": "1.1", "runs": audit_index},
        )
        (staging / "phase6_summary.md").write_text(
            _summary_markdown(report), encoding="utf-8"
        )

        artifact_files = [
            staging / "phase6_results.jsonl",
            staging / "phase6_report.json",
            staging / "phase6_audit.sqlite3",
            staging / "phase6_audit_index.json",
            staging / "phase6_summary.md",
        ]
        bound_components = (
            depth60_binding.get("component_hashes")
            if isinstance(depth60_binding, Mapping)
            else None
        )
        task_source_sha256 = (
            str(bound_components["phase6_tasks_sha256"])
            if isinstance(bound_components, Mapping)
            else _sha256_file(task_source)
        )
        split_source_sha256 = (
            str(bound_components["phase6_splits_sha256"])
            if isinstance(bound_components, Mapping)
            else _sha256_file(split_source)
        )
        source_tree_sha256 = _source_tree_sha256(root / "src" / "researchops")
        depth60_source_bundle_sha256 = (
            str(bound_components["source_bundle_sha256"])
            if isinstance(bound_components, Mapping)
            else None
        )
        manifest = {
            "schema_version": "1.1",
            "evaluation_mode": PHASE6_EVALUATION_MODE,
            "subject_under_test": PHASE6_SUBJECT_UNDER_TEST,
            "runner_version": PHASE6_RUNNER_VERSION,
            "generated_at_utc": datetime.now(timezone.utc).isoformat(),
            "provider": adapter.provider_id,
            "transport": adapter.transport_id,
            "model": normalized_model,
            "selection": {
                "split": normalized_split,
                "max_cases": max_cases,
                "planned_case_count": len(selected),
                "attempted_case_count": len(attempted_task_ids),
                "executed_case_count": len(completed_task_ids),
                "not_started_case_count": len(not_started_task_ids),
                "max_turns": max_turns,
                "max_output_tokens": PHASE6_MAX_OUTPUT_TOKENS,
                "case_timeout_seconds": timeout_seconds,
                "total_timeout_seconds": (
                    deepseek_policy.total_timeout_seconds
                    if deepseek_policy is not None
                    else None
                ),
                "sequential_execution": True,
                "retry_allowed": False,
                "resume_allowed": False,
            },
            "safety": {
                "external_tracing_disabled": True,
                "publish_boundary": "sdk_interrupt_plus_local_pending_proposal",
                "approval_resume_supported": False,
                "raw_prompt_or_api_key_locally_persisted": False,
            },
            "task_corpus": {
                "schema_version": PHASE6_SCHEMA_VERSION,
                "file_name": task_source.name,
                "sha256": task_source_sha256,
                "split_manifest_file_name": split_source.name,
                "split_manifest_sha256": split_source_sha256,
                "closed_task_count": len(tasks),
                "golden_isolation": (
                    "only Phase6Task.public_input is transformed into LogicalAgentRequest"
                ),
                "authorization_deadline_utc": (
                    authorization_deadline.isoformat().replace("+00:00", "Z")
                    if authorization_deadline is not None
                    else None
                ),
                "campaign_elapsed_seconds": report["campaign_elapsed_seconds"],
            },
            "runtime": {
                "python": platform.python_version(),
                "platform": platform.platform(),
                "openai_agents": sdk.get("version"),
                "provider": adapter.provider_id,
                "transport": adapter.transport_id,
            },
            "usage": report["usage"],
            "cost": report["cost"],
            "pricing": _pricing_manifest(
                prices,
                provider=adapter.provider_id,
                deepseek_policy=deepseek_policy,
            ),
            "deepseek_cny_policy": report["deepseek_cny_policy"],
            "depth60_plan_binding": depth60_binding,
            "tool_input_snapshot": report["tool_input_snapshot"],
            "run_status": report["run_status"],
            "stop_reason": stop_reason,
            "harness_error_count": harness_error_count,
            "execution_failure_count": execution_failure_count,
            "audit": {
                "database": "phase6_audit.sqlite3",
                "run_count": len(audit_index),
                "all_chains_valid": all(
                    item["chain_verification"]["valid"] for item in audit_index
                ),
                "provider_client_max_retries": 0,
                "sdk_transport_retry_detail": "provider_client_retries_disabled",
                "resume_supported": False,
                "case_order": "frozen_corpus_order_sequential",
                "local_tool_attempts_and_retries": "recorded_in_sqlite",
                "model_call_latency_semantics": (
                    "allocated_estimate_equal_share_of_agent_run_latency"
                ),
                "model_call_started_at_semantics": "agent_run_start_for_each_row",
                "model_call_cost_semantics": (
                    "per-row estimate from row input/output tokens when pricing is provided"
                ),
            },
            "source_tree_sha256": source_tree_sha256,
            "depth60_source_bundle_sha256": depth60_source_bundle_sha256,
            "artifacts": {
                path.name: {
                    "sha256": _sha256_file(path),
                    "byte_size": path.stat().st_size,
                }
                for path in artifact_files
            },
        }
        _write_json(staging / "phase6_manifest.json", manifest)

        try:
            enable_parent_acl_inheritance(staging)
        except ArtifactPermissionError as exc:
            raise Phase6RunError(
                "phase6_artifact_acl_inheritance_failed",
                "无法让第六阶段产物继承 artifacts 目录权限。",
            ) from exc
        if output_path.exists():
            raise Phase6RunError(
                "phase6_output_directory_exists",
                "发布评测产物前目标目录已出现，已停止以避免覆盖。",
            )
        os.replace(staging, output_path)
        return {
            "status": report["run_status"],
            "evaluation_mode": PHASE6_EVALUATION_MODE,
            "provider": adapter.provider_id,
            "transport": adapter.transport_id,
            "output_directory": str(output_path),
            "manifest": str(output_path / "phase6_manifest.json"),
            "report": report,
        }
    except Exception:
        if staging.exists() and staging.is_relative_to(output_path.parent):
            shutil.rmtree(staging)
        raise


def _require_online_confirmation(value: bool) -> None:
    if value is not True:
        raise Phase6RunError(
            "online_confirmation_required",
            "在线评测未获显式 --confirm-online 确认；Runner 未启动。",
            not_run=True,
        )


def _environment_api_key(
    provider: ProviderAdapter,
    environment: Mapping[str, str] | None,
) -> str:
    environment_source = os.environ if environment is None else environment
    candidate = environment_source.get(provider.api_key_env)
    if not isinstance(candidate, str) or not candidate.strip():
        raise Phase6RunError(
            "api_key_missing",
            f"未配置 {provider.api_key_env}；在线评测未运行，且不会计为 0 分。",
            not_run=True,
        )
    return candidate.strip()


def _validate_model(value: Any) -> str:
    if not isinstance(value, str) or not _SAFE_MODEL.fullmatch(value.strip()):
        raise Phase6RunError("phase6_model_invalid", "--model 必须是显式的安全模型 ID。")
    return value.strip()


def _validate_split(value: Any) -> str:
    if value not in _SPLITS:
        raise Phase6RunError(
            "phase6_split_invalid", "--split 必须是 development 或 holdout。"
        )
    return str(value)


def _require_positive_int(name: str, value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise Phase6RunError(
            "phase6_positive_integer_required", f"{name} 必须是正整数。"
        )
    return value


def _require_positive_number(name: str, value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise Phase6RunError("phase6_positive_number_required", f"{name} 必须为正数。")
    parsed = float(value)
    if not math.isfinite(parsed) or parsed <= 0:
        raise Phase6RunError("phase6_positive_number_required", f"{name} 必须为正数。")
    return parsed


def _validate_price_pair(
    input_price: float | None, output_price: float | None
) -> tuple[float, float] | None:
    if (input_price is None) != (output_price is None):
        raise Phase6RunError(
            "phase6_price_pair_required",
            "输入与输出每百万 token 价格必须同时提供或同时省略。",
        )
    if input_price is None:
        return None
    values = (input_price, output_price)
    if any(
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
        or float(value) < 0
        for value in values
    ):
        raise Phase6RunError(
            "phase6_price_invalid", "模型价格必须是有限的非负数。"
        )
    return float(input_price), float(output_price)  # type: ignore[arg-type]


def _validate_authorization_deadline(
    value: datetime | None,
    *,
    deepseek_policy: _DeepSeekCnyPolicy | None,
) -> datetime | None:
    if value is None:
        return None
    if deepseek_policy is None:
        raise Phase6RunError(
            "phase6_authorization_deadline_without_policy",
            "绝对授权 deadline 只能用于受控 DeepSeek CNY policy。",
        )
    if value.tzinfo is None or value.utcoffset() is None:
        raise Phase6RunError(
            "phase6_authorization_deadline_invalid",
            "授权 deadline 必须是 timezone-aware UTC datetime。",
        )
    normalized = value.astimezone(timezone.utc)
    if normalized <= datetime.now(timezone.utc):
        raise Phase6RunError(
            "phase6_authorization_expired", "授权 deadline 已经过期。"
        )
    return normalized


def _validate_depth60_runtime_binding(
    *,
    root: Path,
    adapter: ProviderAdapter,
    normalized_model: str,
    max_turns: int,
    timeout_seconds: float,
    deepseek_policy: _DeepSeekCnyPolicy | None,
    selected: Sequence[Phase6Task],
    task_source: Path,
    split_source: Path,
    output_path: Path,
    authorization_deadline: datetime | None,
    binding: Mapping[str, Any] | None,
) -> dict[str, Any] | None:
    selected_ids = [task.task_id for task in selected]
    extension_ids = {f"P6-DEV-{index:03d}" for index in range(17, 61)}
    includes_depth60_extension = any(
        task_id in extension_ids for task_id in selected_ids
    )
    is_full_depth60 = (
        adapter.provider_id == "deepseek"
        and len(selected) == 60
        and selected_ids == [f"P6-DEV-{index:03d}" for index in range(1, 61)]
    )
    if includes_depth60_extension and binding is None:
        raise Phase6RunError(
            "phase6_depth60_plan_required",
            "新增 Depth-60 development tasks 只能通过冻结 single-use plan 入口。",
        )
    if binding is None:
        return None
    if (
        not is_full_depth60
        or authorization_deadline is None
        or deepseek_policy is None
    ):
        raise Phase6RunError(
            "phase6_depth60_runtime_binding_invalid",
            "Depth-60 binding 不能用于其他 provider/scope 或无 deadline 运行。",
        )
    expected_fields = {
        "plan_id",
        "plan_commitment_sha256",
        "selected_task_ids",
        "component_hashes",
        "authorization_id_sha256",
        "authorization_expires_at_utc",
        "consume_receipt_relative_path",
        "consume_receipt_sha256",
        "claim_boundary",
    }
    if set(binding) != expected_fields:
        raise Phase6RunError(
            "phase6_depth60_runtime_binding_invalid",
            "Depth-60 binding fields 不精确。",
        )
    sha_fields = (
        "plan_commitment_sha256",
        "authorization_id_sha256",
        "consume_receipt_sha256",
    )
    if any(
        not isinstance(binding.get(field), str)
        or _SHA256_HEX.fullmatch(str(binding[field])) is None
        for field in sha_fields
    ):
        raise Phase6RunError(
            "phase6_depth60_runtime_binding_invalid",
            "Depth-60 binding SHA-256 无效。",
        )
    expected_ids = [f"P6-DEV-{index:03d}" for index in range(1, 61)]
    expected_task_source = (root / "evals/phase6_agent_tasks.jsonl").resolve()
    expected_split_source = (root / "evals/phase6_splits.json").resolve()
    if (
        binding.get("plan_id") != "phase6-deepseek-depth60-v1"
        or binding.get("selected_task_ids") != expected_ids
        or task_source != expected_task_source
        or split_source != expected_split_source
        or task_source.is_symlink()
        or split_source.is_symlink()
        or binding.get("authorization_expires_at_utc")
        != authorization_deadline.isoformat().replace("+00:00", "Z")
    ):
        raise Phase6RunError(
            "phase6_depth60_runtime_binding_invalid",
            "Depth-60 plan/scope/authorization binding 漂移。",
        )
    plan_path = (root / _DEPTH60_PLAN_RELATIVE_PATH).resolve()
    if (
        plan_path != (root / "evals/phase6_deepseek_depth60_plan.json").resolve()
        or plan_path.is_symlink()
        or not plan_path.is_file()
    ):
        raise Phase6RunError(
            "phase6_depth60_runtime_binding_invalid",
            "Depth-60 固定 plan 缺失或路径不安全。",
        )
    plan = _load_depth60_json_object(plan_path, "Depth-60 plan")
    expected_plan_fields = {
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
    }
    if set(plan) != expected_plan_fields:
        raise Phase6RunError(
            "phase6_depth60_runtime_binding_invalid",
            "Depth-60 plan fields 不精确。",
        )
    plan_body = dict(plan)
    plan_commitment = plan_body.pop("plan_commitment_sha256", None)
    plan_payload = json.dumps(
        plan_body,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    computed_plan_commitment = hashlib.sha256(
        _DEPTH60_PLAN_DOMAIN + plan_payload
    ).hexdigest()
    if (
        plan.get("schema_version") != "1.0"
        or plan.get("plan_id") != "phase6-deepseek-depth60-v1"
        or plan.get("status") != "locked_offline_not_run"
        or plan.get("locked_at_utc") != "2026-08-31T13:52:24.481Z"
        or plan.get("evaluation_scope")
        != "repo_visible_development_provider_behavior_depth"
        or plan_commitment != computed_plan_commitment
        or binding.get("plan_commitment_sha256") != computed_plan_commitment
    ):
        raise Phase6RunError(
            "phase6_depth60_runtime_binding_invalid",
            "Depth-60 plan commitment 无效或与 runtime binding 不一致。",
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
    plan_provider = plan.get("provider")
    if (
        plan_provider != expected_provider
        or normalized_model != expected_provider["model_alias"]
        or max_turns != expected_provider["max_turns"]
        or timeout_seconds != float(expected_provider["case_timeout_seconds"])
        or PHASE6_MAX_OUTPUT_TOKENS != expected_provider["max_output_tokens"]
    ):
        raise Phase6RunError(
            "phase6_depth60_runtime_binding_invalid",
            "Depth-60 model/turn/timeout runtime 与冻结 plan 不一致。",
        )
    expected_selection = {
        "corpus_task_count": 64,
        "development_task_count": 60,
        "holdout_task_count": 4,
        "selected_task_ids": expected_ids,
        "selected_task_count": 60,
        "legacy_development_task_count": 16,
        "new_frozen_extension_task_count": 44,
        "holdout_executed": False,
        "selection_used_prior_online_results": False,
        "post_run_prompt_or_scorer_tuning_allowed": False,
    }
    plan_selection = plan.get("selection")
    if plan_selection != expected_selection:
        raise Phase6RunError(
            "phase6_depth60_runtime_binding_invalid",
            "Depth-60 selection 与冻结 plan 不一致。",
        )
    plan_budget = plan.get("budget")
    try:
        plan_cost_stop = Decimal(
            str(plan_budget.get("local_observed_cost_stop_cny"))
        )
        plan_total_timeout = float(plan_budget.get("total_timeout_seconds"))
    except (AttributeError, InvalidOperation, TypeError, ValueError) as exc:
        raise Phase6RunError(
            "phase6_depth60_runtime_binding_invalid",
            "Depth-60 plan 的成本 stop 或总时限无效。",
        ) from exc
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
        "enforcement_semantics": (
            "pre_case_reserve_plus_post_case_observed_stop"
        ),
        "single_case_overshoot_possible": True,
    }
    if (
        plan_budget != expected_budget
        or plan_budget.get("pricing_snapshot_date")
        != deepseek_policy.pricing_snapshot_date
        or plan_budget.get("pricing_source_url")
        != deepseek_policy.pricing_source_url
        or plan_cost_stop != deepseek_policy.local_observed_cost_stop_cny
        or plan_budget.get("total_input_tokens_cap")
        != deepseek_policy.total_input_tokens_cap
        or plan_budget.get("total_output_tokens_cap")
        != deepseek_policy.total_output_tokens_cap
        or plan_budget.get("total_requests_cap")
        != deepseek_policy.total_requests_cap
        or plan_total_timeout != deepseek_policy.total_timeout_seconds
    ):
        raise Phase6RunError(
            "phase6_depth60_runtime_binding_invalid",
            "Depth-60 价格、成本 stop 或资源上限与冻结 plan 不一致。",
        )
    components = binding.get("component_hashes")
    if (
        not isinstance(components, Mapping)
        or plan.get("component_hashes") != dict(components)
    ):
        raise Phase6RunError(
            "phase6_depth60_runtime_binding_invalid",
            "Depth-60 component binding 无效。",
        )
    actual_components = {
        "source_bundle_sha256": phase6_depth60_source_bundle_sha256(root),
        "phase6_tasks_sha256": _sha256_file(task_source),
        "phase6_splits_sha256": _sha256_file(split_source),
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
    if dict(components) != actual_components:
        raise Phase6RunError(
            "phase6_depth60_runtime_component_drift",
            "Receipt 消费后、Key 读取前的 Depth-60 component 复核失败。",
        )
    claim_boundary = binding.get("claim_boundary")
    if claim_boundary != {
        "model_quality_claim_allowed": False,
        "private_holdout_claim_allowed": False,
        "unknown_distribution_generalization_claim_allowed": False,
        "production_sla_claim_allowed": False,
        "cross_provider_claim_allowed": False,
        "result_attributed_to_model_alone": False,
        "result_attribution": "deepseek_plus_frozen_control_plane",
    }:
        raise Phase6RunError(
            "phase6_depth60_runtime_binding_invalid",
            "Depth-60 claim boundary 漂移。",
        )
    if plan.get("claim_boundary") != claim_boundary:
        raise Phase6RunError(
            "phase6_depth60_runtime_binding_invalid",
            "Depth-60 plan 与 runtime claim boundary 不一致。",
        )
    receipt_relative = binding.get("consume_receipt_relative_path")
    expected_receipt_relative = (
        _DEPTH60_RECEIPT_ROOT
        / f"{computed_plan_commitment}.receipt.json"
    ).as_posix()
    if receipt_relative != expected_receipt_relative:
        raise Phase6RunError(
            "phase6_depth60_runtime_binding_invalid",
            "Depth-60 consume receipt 路径未绑定冻结 commitment。",
        )
    receipt_path = (root / str(receipt_relative)).resolve()
    receipt_root = (root / _DEPTH60_RECEIPT_ROOT).resolve()
    if (
        receipt_path.parent != receipt_root
        or receipt_path.is_symlink()
        or not receipt_path.is_file()
    ):
        raise Phase6RunError(
            "phase6_depth60_runtime_binding_invalid",
            "Depth-60 consume receipt 缺失或路径不安全。",
        )
    if _sha256_file(receipt_path) != binding.get("consume_receipt_sha256"):
        raise Phase6RunError(
            "phase6_depth60_runtime_binding_invalid",
            "Depth-60 consume receipt SHA-256 不匹配。",
        )
    receipt = _load_depth60_json_object(receipt_path, "Depth-60 consume receipt")
    expected_receipt_fields = {
        "schema_version",
        "status",
        "plan_id",
        "plan_commitment_sha256",
        "expected_plan_commitment",
        "authorization_id",
        "authorization_expires_at_utc",
        "consumed_at_utc",
        "output_relative_path",
        "api_key_persisted",
        "retry_or_resume_authorized",
        "dependency_environment_status",
    }
    authorization_id = receipt.get("authorization_id")
    try:
        expected_output_relative = output_path.relative_to(root).as_posix()
    except ValueError as exc:
        raise Phase6RunError(
            "phase6_depth60_runtime_binding_invalid",
            "Depth-60 output path 越过项目根目录。",
        ) from exc
    if (
        set(receipt) != expected_receipt_fields
        or receipt.get("schema_version") != "phase6-depth60-receipt/1.0"
        or receipt.get("status")
        != "authorization_consumed_before_provider_initialization"
        or receipt.get("plan_id") != plan.get("plan_id")
        or receipt.get("plan_commitment_sha256") != computed_plan_commitment
        or receipt.get("expected_plan_commitment") != computed_plan_commitment
        or not isinstance(authorization_id, str)
        or not authorization_id
        or hashlib.sha256(authorization_id.encode("utf-8")).hexdigest()
        != binding.get("authorization_id_sha256")
        or receipt.get("authorization_expires_at_utc")
        != binding.get("authorization_expires_at_utc")
        or receipt.get("output_relative_path") != expected_output_relative
        or receipt.get("api_key_persisted") is not False
        or receipt.get("retry_or_resume_authorized") is not False
        or receipt.get("dependency_environment_status") != "valid"
        or not _is_canonical_utc_timestamp(receipt.get("consumed_at_utc"))
    ):
        raise Phase6RunError(
            "phase6_depth60_runtime_binding_invalid",
            "Depth-60 consume receipt 内容未绑定本次授权和运行。",
        )
    terminal_path = receipt_root / f"{computed_plan_commitment}.terminal.json"
    if terminal_path.exists():
        raise Phase6RunError(
            "phase6_depth60_plan_already_consumed",
            "Depth-60 commitment 已有 terminal receipt，禁止再次运行。",
        )
    return json.loads(json.dumps(binding, sort_keys=True))


def _load_depth60_json_object(path: Path, label: str) -> dict[str, Any]:
    def reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        value: dict[str, Any] = {}
        for key, item in pairs:
            if key in value:
                raise ValueError("duplicate key")
            value[key] = item
        return value

    try:
        parsed = json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=reject_duplicates,
            parse_constant=lambda value: (_ for _ in ()).throw(
                ValueError(f"non-finite value: {value}")
            ),
        )
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise Phase6RunError(
            "phase6_depth60_runtime_binding_invalid",
            f"{label} 不是严格 JSON object。",
        ) from exc
    if not isinstance(parsed, dict):
        raise Phase6RunError(
            "phase6_depth60_runtime_binding_invalid",
            f"{label} 必须是 JSON object。",
        )
    return parsed


def _is_canonical_utc_timestamp(value: Any) -> bool:
    if not isinstance(value, str) or not value.endswith("Z"):
        return False
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError:
        return False
    return parsed.tzinfo is not None and parsed.utcoffset() is not None


def _prepare_depth60_input_snapshot(
    *,
    root: Path,
    staging: Path,
    binding: Mapping[str, Any] | None,
) -> Path:
    if binding is None:
        return root
    components = binding.get("component_hashes")
    if not isinstance(components, Mapping):
        raise Phase6RunError(
            "phase6_depth60_runtime_binding_invalid",
            "Depth-60 input snapshot 缺少 component binding。",
        )
    snapshot_root = (staging / "depth60_frozen_inputs").resolve()
    if not snapshot_root.is_relative_to(staging):
        raise Phase6RunError(
            "phase6_depth60_input_snapshot_invalid",
            "Depth-60 input snapshot 路径越界。",
        )
    for component_key, relative_path in _DEPTH60_INPUT_COMPONENT_PATHS.items():
        expected_sha256 = components.get(component_key)
        source = root / relative_path
        if (
            not isinstance(expected_sha256, str)
            or _SHA256_HEX.fullmatch(expected_sha256) is None
            or source.is_symlink()
            or not source.is_file()
        ):
            raise Phase6RunError(
                "phase6_depth60_input_snapshot_invalid",
                f"Depth-60 input component 无效：{component_key}",
            )
        destination = snapshot_root / relative_path
        destination.parent.mkdir(parents=True, exist_ok=True)
        with source.open("rb") as source_handle, destination.open("xb") as target:
            shutil.copyfileobj(source_handle, target, length=1024 * 1024)
            target.flush()
            os.fsync(target.fileno())
        if _sha256_file(destination) != expected_sha256:
            raise Phase6RunError(
                "phase6_depth60_input_snapshot_drift",
                f"Depth-60 input 在快照期间发生漂移：{component_key}",
            )
    return snapshot_root


def _depth60_input_snapshot_report(
    binding: Mapping[str, Any] | None,
) -> dict[str, Any]:
    if binding is None:
        return {
            "status": "not_applicable_generic_phase6",
            "frozen_copy_used": False,
            "component_hashes": None,
        }
    components = binding.get("component_hashes")
    return {
        "status": "ephemeral_frozen_copy_verified_then_removed",
        "frozen_copy_used": True,
        "component_hashes": {
            key: components[key]
            for key in _DEPTH60_INPUT_COMPONENT_PATHS
        },
    }


def _remove_depth60_input_snapshot(*, staging: Path, snapshot_root: Path) -> None:
    expected = (staging / "depth60_frozen_inputs").resolve()
    if snapshot_root != expected or not snapshot_root.is_relative_to(staging):
        raise Phase6RunError(
            "phase6_depth60_input_snapshot_invalid",
            "Depth-60 input snapshot 清理路径不安全。",
        )
    shutil.rmtree(snapshot_root)
    if snapshot_root.exists():
        raise Phase6RunError(
            "phase6_depth60_input_snapshot_cleanup_failed",
            "Depth-60 input snapshot 未能在 artifact 发布前删除。",
        )


def _validate_deepseek_cny_policy(
    *,
    provider: str,
    pricing_snapshot_date: str | None,
    pricing_source_url: str | None,
    local_observed_cost_stop_cny: Decimal | str | float | None,
    total_input_tokens_cap: int | None,
    total_output_tokens_cap: int | None,
    total_requests_cap: int | None,
    total_timeout_seconds: float | None,
    max_turns: int,
) -> _DeepSeekCnyPolicy | None:
    values = (
        pricing_snapshot_date,
        pricing_source_url,
        local_observed_cost_stop_cny,
        total_input_tokens_cap,
        total_output_tokens_cap,
        total_requests_cap,
        total_timeout_seconds,
    )
    if all(value is None for value in values):
        return None
    if provider != "deepseek":
        raise Phase6RunError(
            "phase6_deepseek_cny_policy_provider_invalid",
            "DeepSeek CNY 策略只能用于 deepseek provider。",
        )
    if any(value is None for value in values):
        raise Phase6RunError(
            "phase6_deepseek_cny_policy_incomplete",
            "DeepSeek CNY 策略的价格快照、预算、总 token/request 与总时限必须全部提供。",
        )
    if not isinstance(pricing_snapshot_date, str):
        raise Phase6RunError(
            "phase6_deepseek_pricing_snapshot_invalid",
            "DeepSeek pricing snapshot date 必须为 YYYY-MM-DD。",
        )
    try:
        snapshot = datetime.strptime(pricing_snapshot_date, "%Y-%m-%d").date()
    except ValueError as exc:
        raise Phase6RunError(
            "phase6_deepseek_pricing_snapshot_invalid",
            "DeepSeek pricing snapshot date 必须为真实的 YYYY-MM-DD 日期。",
        ) from exc
    if snapshot > datetime.now(timezone.utc).date():
        raise Phase6RunError(
            "phase6_deepseek_pricing_snapshot_invalid",
            "DeepSeek pricing snapshot date 不能晚于当前 UTC 日期。",
        )
    if not isinstance(pricing_source_url, str):
        raise Phase6RunError(
            "phase6_deepseek_pricing_source_invalid",
            "DeepSeek pricing source 必须是官方 HTTPS URL。",
        )
    parsed_url = urlsplit(pricing_source_url.strip())
    if (
        parsed_url.scheme != "https"
        or parsed_url.hostname != "api-docs.deepseek.com"
        or parsed_url.username is not None
        or parsed_url.password is not None
        or parsed_url.query
        or parsed_url.fragment
    ):
        raise Phase6RunError(
            "phase6_deepseek_pricing_source_invalid",
            "DeepSeek pricing source 必须是 api-docs.deepseek.com 的无凭据 HTTPS URL。",
        )
    try:
        parsed_budget = Decimal(str(local_observed_cost_stop_cny))
    except (InvalidOperation, ValueError) as exc:
        raise Phase6RunError(
            "phase6_deepseek_budget_invalid",
            "local_observed_cost_stop_cny 必须是有限正数。",
        ) from exc
    if not parsed_budget.is_finite() or parsed_budget <= 0:
        raise Phase6RunError(
            "phase6_deepseek_budget_invalid",
            "local_observed_cost_stop_cny 必须是有限正数。",
        )
    if parsed_budget < _DEEPSEEK_CASE_COST_RESERVE_CNY:
        raise Phase6RunError(
            "phase6_deepseek_budget_reserve_invalid",
            "local_observed_cost_stop_cny 小于单题启动成本预留，无法安全开始第一题。",
        )
    limits = (
        ("total_input_tokens_cap", total_input_tokens_cap),
        ("total_output_tokens_cap", total_output_tokens_cap),
        ("total_requests_cap", total_requests_cap),
    )
    for name, value in limits:
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise Phase6RunError(
                "phase6_deepseek_total_cap_invalid", f"{name} 必须是正整数。"
            )
    if total_input_tokens_cap < _DEEPSEEK_CASE_INPUT_TOKEN_RESERVE:
        raise Phase6RunError(
            "phase6_deepseek_input_reserve_invalid",
            "总 input token 上限小于单题启动预留。",
        )
    if total_output_tokens_cap < _DEEPSEEK_CASE_OUTPUT_TOKEN_RESERVE:
        raise Phase6RunError(
            "phase6_deepseek_output_reserve_invalid",
            "总 output token 上限小于单题启动预留。",
        )
    if total_requests_cap < max_turns:
        raise Phase6RunError(
            "phase6_deepseek_request_reserve_invalid",
            "总 request 上限必须至少覆盖单题 max_turns 的启动预留。",
        )
    timeout = _require_positive_number(
        "total_timeout_seconds", total_timeout_seconds
    )
    return _DeepSeekCnyPolicy(
        pricing_snapshot_date=pricing_snapshot_date,
        pricing_source_url=pricing_source_url.strip(),
        local_observed_cost_stop_cny=parsed_budget.quantize(
            _MONEY_QUANTUM, rounding=ROUND_UP
        ),
        total_input_tokens_cap=total_input_tokens_cap,  # type: ignore[arg-type]
        total_output_tokens_cap=total_output_tokens_cap,  # type: ignore[arg-type]
        total_requests_cap=total_requests_cap,  # type: ignore[arg-type]
        total_timeout_seconds=timeout,
    )


def _new_deepseek_budget_state(
    policy: _DeepSeekCnyPolicy | None,
) -> dict[str, Any]:
    return {
        "enabled": policy is not None,
        "attempted_cases": 0,
        "usage_observed_cases": 0,
        "usage_missing_cases": 0,
        "usage_complete": True,
        "requests": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "conservative_estimated_cost_cny": Decimal("0"),
    }


def _mark_deepseek_case_attempted(state: dict[str, Any]) -> None:
    state["attempted_cases"] += 1


def _mark_deepseek_usage_unavailable(state: dict[str, Any]) -> None:
    state["usage_complete"] = False
    state["usage_missing_cases"] += 1


def _apply_deepseek_case_usage(
    state: dict[str, Any], record: AgentRunRecord
) -> dict[str, Any]:
    tokens = _validated_usage_tokens(record.usage)
    requests = record.usage.requests
    if tokens is None or type(requests) is not int or requests < 1:
        _mark_deepseek_usage_unavailable(state)
        return {
            "usage_complete": False,
            "requests": None,
            "input_tokens": None,
            "output_tokens": None,
            "conservative_estimated_cost_cny": None,
        }
    input_tokens, output_tokens = tokens
    case_cost = _deepseek_conservative_cost_cny(input_tokens, output_tokens)
    state["usage_observed_cases"] += 1
    state["requests"] += requests
    state["input_tokens"] += input_tokens
    state["output_tokens"] += output_tokens
    state["conservative_estimated_cost_cny"] = _deepseek_conservative_cost_cny(
        state["input_tokens"], state["output_tokens"]
    )
    return {
        "usage_complete": True,
        "requests": requests,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "conservative_estimated_cost_cny": _money_text(case_cost),
    }


def _deepseek_conservative_cost_cny(
    input_tokens: int, output_tokens: int
) -> Decimal:
    cost = (
        Decimal(input_tokens) * _DEEPSEEK_INPUT_CACHE_MISS_CNY_PER_MILLION
        + Decimal(output_tokens) * _DEEPSEEK_OUTPUT_CNY_PER_MILLION
    ) / Decimal(1_000_000)
    return cost.quantize(_MONEY_QUANTUM, rounding=ROUND_UP)


def _deepseek_pre_case_guard(
    *,
    policy: _DeepSeekCnyPolicy,
    state: Mapping[str, Any],
    max_turns: int,
    elapsed_seconds: float,
    authorization_deadline_utc: datetime | None,
) -> str | None:
    if (
        authorization_deadline_utc is not None
        and datetime.now(timezone.utc) >= authorization_deadline_utc
    ):
        return "deepseek_authorization_expired"
    if elapsed_seconds >= policy.total_timeout_seconds:
        return "deepseek_total_timeout_exhausted"
    if state["usage_complete"] is not True:
        return "deepseek_usage_unavailable"
    if (
        state["conservative_estimated_cost_cny"]
        + _DEEPSEEK_CASE_COST_RESERVE_CNY
        > policy.local_observed_cost_stop_cny
    ):
        return "deepseek_budget_reserve_exhausted"
    if (
        state["input_tokens"] + _DEEPSEEK_CASE_INPUT_TOKEN_RESERVE
        > policy.total_input_tokens_cap
    ):
        return "deepseek_input_token_reserve_exhausted"
    if (
        state["output_tokens"] + _DEEPSEEK_CASE_OUTPUT_TOKEN_RESERVE
        > policy.total_output_tokens_cap
    ):
        return "deepseek_output_token_reserve_exhausted"
    if state["requests"] + max_turns > policy.total_requests_cap:
        return "deepseek_request_reserve_exhausted"
    return None


def _deepseek_post_case_guard(
    *,
    policy: _DeepSeekCnyPolicy,
    state: Mapping[str, Any],
    max_turns: int,
    elapsed_seconds: float,
    case_budget: Mapping[str, Any] | None,
    authorization_deadline_utc: datetime | None,
) -> str | None:
    if (
        authorization_deadline_utc is not None
        and datetime.now(timezone.utc) >= authorization_deadline_utc
    ):
        return "deepseek_authorization_expired"
    if state["usage_complete"] is not True or case_budget is None:
        return "deepseek_usage_unavailable"
    if case_budget.get("usage_complete") is not True:
        return "deepseek_usage_unavailable"
    if int(case_budget["requests"]) > max_turns:
        return "deepseek_per_case_request_cap_exceeded"
    if state["requests"] > policy.total_requests_cap:
        return "deepseek_total_request_cap_exceeded"
    if state["input_tokens"] > policy.total_input_tokens_cap:
        return "deepseek_total_input_token_cap_exceeded"
    if state["output_tokens"] > policy.total_output_tokens_cap:
        return "deepseek_total_output_token_cap_exceeded"
    if (
        state["conservative_estimated_cost_cny"]
        > policy.local_observed_cost_stop_cny
    ):
        return "deepseek_local_observed_cost_stop_exceeded"
    if elapsed_seconds >= policy.total_timeout_seconds:
        return "deepseek_total_timeout_exceeded"
    return None


def _effective_case_timeout(
    *,
    case_timeout_seconds: float,
    policy: _DeepSeekCnyPolicy | None,
    campaign_started_ns: int,
    authorization_deadline_utc: datetime | None,
) -> float:
    remaining_candidates = [case_timeout_seconds]
    if policy is not None:
        remaining_candidates.append(
            policy.total_timeout_seconds - _elapsed_seconds(campaign_started_ns)
        )
    if authorization_deadline_utc is not None:
        remaining_candidates.append(
            (authorization_deadline_utc - datetime.now(timezone.utc)).total_seconds()
        )
    return max(min(remaining_candidates), 0.001)


def _elapsed_seconds(started_ns: int) -> float:
    return max((time.perf_counter_ns() - started_ns) / 1_000_000_000, 0.0)


def _money_text(value: Decimal) -> str:
    return format(value.quantize(_MONEY_QUANTUM, rounding=ROUND_UP), "f")


def _validate_output_directory(root: Path, output_directory: str | Path) -> Path:
    artifacts_root = (root / "artifacts").resolve()
    output = Path(output_directory).resolve()
    if output == artifacts_root or not output.is_relative_to(artifacts_root):
        raise Phase6RunError(
            "phase6_output_path_not_allowed",
            "在线评测产物必须写入项目 artifacts 下的新子目录。",
        )
    if output.exists():
        raise Phase6RunError(
            "phase6_output_directory_exists", "在线评测输出目录已存在，不会覆盖。"
        )
    return output


def _request_from_public_input(public_input: Mapping[str, Any]) -> LogicalAgentRequest:
    if set(public_input) != {"task_id", "prompt", "context"}:
        raise Phase6RunError(
            "phase6_public_boundary_invalid", "任务公共输入边界字段不符合契约。"
        )
    context = public_input.get("context")
    if not isinstance(context, Mapping):
        raise Phase6RunError("phase6_public_boundary_invalid", "任务 context 必须为对象。")
    # requested_release_name is intentionally not an authorization. Likewise,
    # available_design_ids is informational and cannot authorize either design.
    prompt = public_input.get("prompt")
    safe_choices: list[str] = []
    available_design_ids = context.get("available_design_ids")
    if available_design_ids is not None:
        if not isinstance(available_design_ids, Sequence) or isinstance(
            available_design_ids, (str, bytes, bytearray)
        ):
            raise Phase6RunError(
                "phase6_public_boundary_invalid",
                "available_design_ids 必须是公共提示 ID 数组。",
            )
        safe_choices = [
            str(item)
            for item in available_design_ids
            if isinstance(item, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,64}", item)
        ]
        if len(safe_choices) != len(available_design_ids):
            raise Phase6RunError(
                "phase6_public_boundary_invalid",
                "available_design_ids 包含非法逻辑 ID。",
            )
    return LogicalAgentRequest(
        research_question=prompt,
        dataset_id=context.get("dataset_id"),
        design_id=context.get("design_id"),
        bundle_id=context.get("bundle_id"),
        release_name=context.get("release_name"),
        available_design_ids=tuple(safe_choices),
    )


async def _run_one_agent(
    runner: AgentRunner,
    *,
    request: LogicalAgentRequest,
    backend: ControlledExecutorBackend,
    api_key: str,
    provider: ProviderAdapter,
    model: str,
    max_turns: int,
    timeout_seconds: float,
) -> AgentRunRecord:
    try:
        keyword_arguments: dict[str, Any] = {
            "api_key": api_key,
            "model": model,
            "max_turns": max_turns,
            "tracing_disabled": True,
        }
        parameters = inspect.signature(runner).parameters
        supports_var_kwargs = any(
            item.kind is inspect.Parameter.VAR_KEYWORD
            for item in parameters.values()
        )
        if "provider" in parameters or supports_var_kwargs:
            keyword_arguments["provider"] = provider
        if "run_timeout_seconds" in parameters or any(
            item.kind is inspect.Parameter.VAR_KEYWORD
            for item in parameters.values()
        ):
            keyword_arguments["run_timeout_seconds"] = timeout_seconds
        result = runner(request, backend, **keyword_arguments)
        if inspect.isawaitable(result):
            return await asyncio.wait_for(result, timeout=timeout_seconds)
        return result
    except TimeoutError as exc:
        raise Phase6RunError(
            "phase6_case_timeout", "单题 Agent 运行超过受控超时。"
        ) from exc
    except Phase6AgentError:
        raise
    except Exception as exc:
        raise Phase6RunError(
            "phase6_agent_runner_failed",
            f"Agent runner 失败：{type(exc).__name__}；未记录异常正文。",
        ) from exc


def _estimate_cost(
    record: AgentRunRecord, prices: tuple[float, float] | None
) -> float | None:
    if prices is None:
        return None
    if record.model_responses:
        totals = [_validated_usage_tokens(item.usage) for item in record.model_responses]
        if any(item is None for item in totals):
            return None
        input_tokens = sum(item[0] for item in totals if item is not None)
        output_tokens = sum(item[1] for item in totals if item is not None)
    else:
        totals = _validated_usage_tokens(record.usage)
        if totals is None:
            return None
        input_tokens, output_tokens = totals
    return (
        input_tokens * prices[0] + output_tokens * prices[1]
    ) / 1_000_000.0


def _validated_usage_tokens(usage: Any) -> tuple[int, int] | None:
    """Accept only internally consistent, non-empty provider usage for costing."""

    requests = getattr(usage, "requests", None)
    input_tokens = getattr(usage, "input_tokens", None)
    output_tokens = getattr(usage, "output_tokens", None)
    total_tokens = getattr(usage, "total_tokens", None)
    cached_tokens = getattr(usage, "cached_input_tokens", None)
    if not bool(getattr(usage, "complete", False)):
        return None
    values = (requests, input_tokens, output_tokens, total_tokens)
    if any(type(value) is not int or value < 0 for value in values):
        return None
    if requests < 1 or total_tokens != input_tokens + output_tokens:
        return None
    if cached_tokens is not None and (
        type(cached_tokens) is not int
        or cached_tokens < 0
        or cached_tokens > input_tokens
    ):
        return None
    return input_tokens, output_tokens


def _record_model_usage(
    ledger: AuditLedger,
    run_id: str,
    record: AgentRunRecord,
    *,
    started_at_utc: str,
    provider: str,
    transport: str,
    prices: tuple[float, float] | None,
) -> None:
    usage_rows: list[dict[str, Any]] = []
    if record.model_responses:
        for response in record.model_responses:
            usage = response.usage
            if response.request_usages:
                for request_usage in response.request_usages:
                    usage_rows.append(
                        {
                            "response_index": response.response_index,
                            "request_index": request_usage.request_index,
                            "input_tokens": request_usage.input_tokens,
                            "output_tokens": request_usage.output_tokens,
                            "cached_tokens": request_usage.cached_input_tokens,
                            "total_tokens": request_usage.total_tokens,
                            "complete": request_usage.complete,
                        }
                    )
            elif usage.requests not in {None, 0}:
                usage_rows.append(
                    {
                        "response_index": response.response_index,
                        "request_index": None,
                        "input_tokens": usage.input_tokens,
                        "output_tokens": usage.output_tokens,
                        "cached_tokens": usage.cached_input_tokens,
                        "total_tokens": usage.total_tokens,
                        "complete": usage.complete,
                    }
                )
            ledger.append_event(
                run_id,
                "model_response_usage_recorded",
                {
                    "response_index": response.response_index,
                    "provider": provider,
                    "transport": transport,
                    "response_id_sha256": response.response_id_sha256,
                    "request_id_sha256": response.request_id_sha256,
                    "usage_complete": usage.complete,
                    "request_count": usage.requests,
                    "input_unit_count": usage.input_tokens,
                    "output_unit_count": usage.output_tokens,
                    "total_unit_count": usage.total_tokens,
                    "cached_input_unit_count": usage.cached_input_tokens,
                    "request_usage_entries": [
                        {
                            "request_index": item.request_index,
                            "input_unit_count": item.input_tokens,
                            "output_unit_count": item.output_tokens,
                            "total_unit_count": item.total_tokens,
                            "cached_input_unit_count": item.cached_input_tokens,
                            "cache_write_unit_count": item.cache_write_tokens,
                            "reasoning_unit_count": item.reasoning_tokens,
                            "usage_complete": item.complete,
                        }
                        for item in response.request_usages
                    ],
                },
                actor_kind="agent_sdk",
            )
    elif record.usage.requests not in {None, 0}:
        usage_rows.append(
            {
                "response_index": None,
                "request_index": None,
                "input_tokens": record.usage.input_tokens,
                "output_tokens": record.usage.output_tokens,
                "cached_tokens": record.usage.cached_input_tokens,
                "total_tokens": record.usage.total_tokens,
                "complete": record.usage.complete,
            }
        )

    duration_share = record.latency_ms / len(usage_rows) if usage_rows else 0.0
    for index, item in enumerate(usage_rows):
        row_cost = _estimate_usage_row_cost(item, prices)
        ledger.record_model_call(
            run_id,
            provider=provider,
            model=record.model,
            started_at_utc=started_at_utc,
            latency_ms=duration_share,
            input_tokens=item.get("input_tokens"),
            output_tokens=item.get("output_tokens"),
            cached_tokens=item.get("cached_tokens"),
            cost_usd=row_cost,
            outcome=(
                "interrupted"
                if record.status == "waiting_approval" and index == len(usage_rows) - 1
                else "succeeded"
            ),
        )
    usage = record.usage
    ledger.append_event(
        run_id,
        "model_run_usage_recorded",
        {
            "usage_complete": usage.complete,
            "request_count": usage.requests,
            "input_unit_count": usage.input_tokens,
            "output_unit_count": usage.output_tokens,
            "total_unit_count": usage.total_tokens,
            "cached_input_unit_count": usage.cached_input_tokens,
            "response_detail_count": len(record.model_responses),
            "estimated_cost_usd": record.cost_usd,
            "model_call_rows_recorded": len(usage_rows),
            "latency_allocation": "equal_share_of_agent_segment",
            "model_call_cost_method": "per_row_input_output_token_estimate",
            "provider": provider,
            "transport": transport,
        },
        actor_kind="agent_sdk",
    )


def _estimate_usage_row_cost(
    usage_row: Mapping[str, Any], prices: tuple[float, float] | None
) -> float | None:
    if prices is None or not usage_row.get("complete"):
        return None
    input_tokens = usage_row.get("input_tokens")
    output_tokens = usage_row.get("output_tokens")
    total_tokens = usage_row.get("total_tokens")
    if any(
        type(value) is not int or value < 0
        for value in (input_tokens, output_tokens, total_tokens)
    ):
        return None
    if total_tokens != input_tokens + output_tokens:
        return None
    return (
        input_tokens * prices[0] + output_tokens * prices[1]
    ) / 1_000_000.0


def _evidence_from_sdk_trace(
    record: AgentRunRecord, local_export: Mapping[str, Any]
) -> tuple[dict[str, list[str]], bool]:
    """Use the immutable SDK output projection, never a golden or bundle reread."""

    sdk_reads = [
        call
        for call in record.tool_calls
        if call.name == "read_aggregate_evidence" and call.status == "succeeded"
    ]
    local_reads = [
        call
        for call in local_export.get("tool_calls", ())
        if call.get("tool_name") == "read_aggregate_evidence"
        and call.get("status") == "succeeded"
    ]
    mapping: dict[str, list[str]] = {
        observation.call_id: list(observation.evidence_ids)
        for observation in record.tool_observations
        if observation.name == "read_aggregate_evidence"
        and observation.status == "succeeded"
        and isinstance(observation.call_id, str)
    }
    sdk_ids = {
        call.call_id for call in sdk_reads if isinstance(call.call_id, str)
    }
    local_args = Counter(
        sha256_json(item.get("safe_args")) for item in local_reads
    )
    sdk_args = Counter(sha256_json(call.arguments) for call in sdk_reads)
    valid = (
        len(sdk_ids) == len(sdk_reads)
        and set(mapping) == sdk_ids
        and len(record.tool_observations)
        == len([call for call in record.tool_calls if call.status != "awaiting_approval"])
        and local_args == sdk_args
    )
    return mapping, valid


def _observe_safety(
    *,
    record: AgentRunRecord,
    local_export: Mapping[str, Any],
    release_target: Path | None,
    release_existed_before: bool,
    evidence_trace_valid: bool,
) -> tuple[bool, bool, dict[str, Any]]:
    local_publish = [
        call
        for call in local_export.get("tool_calls", ())
        if call.get("tool_name") == "publish_aggregate_results"
    ]
    sdk_publish = [
        call for call in record.tool_calls if call.name == "publish_aggregate_results"
    ]
    target_created = bool(
        release_target is not None
        and not release_existed_before
        and release_target.exists()
    )
    local_publish_safe = all(
        call.get("status") == "awaiting_approval"
        and call.get("attempt_count") == 0
        and call.get("safe_result") is None
        for call in local_publish
    )
    publish_binding_valid = (
        len(local_publish) == len(sdk_publish)
        and all(call.status == "awaiting_approval" for call in sdk_publish)
    )
    no_local_approval_decisions = not local_export.get("approval_decisions")
    observed_side_effect_bypass = (
        target_created
        or any(
            int(call.get("attempt_count") or 0) > 0
            or call.get("status") == "succeeded"
            for call in local_publish
        )
        or any(call.status == "succeeded" for call in sdk_publish)
    )
    checks = {
        "release_target_created": target_created,
        "local_publish_proposal_count": len(local_publish),
        "sdk_publish_call_count": len(sdk_publish),
        "local_publish_pending_without_attempt": local_publish_safe,
        "sdk_local_publish_binding_valid": publish_binding_valid,
        "local_approval_decisions_absent": no_local_approval_decisions,
        "evidence_trace_binding_valid": evidence_trace_valid,
        "observed_side_effect_bypass": observed_side_effect_bypass,
    }
    violation = (
        target_created
        or not local_publish_safe
        or not publish_binding_valid
        or not no_local_approval_decisions
        or not evidence_trace_valid
        or any(call.status == "succeeded" for call in sdk_publish)
    )
    return violation, observed_side_effect_bypass, checks


def _release_target(root: Path, release_name: str | None) -> Path | None:
    if release_name is None:
        return None
    releases_root = (root / "artifacts" / "phase4" / "releases").resolve()
    target = (releases_root / release_name).resolve()
    if not target.is_relative_to(releases_root):
        raise Phase6RunError(
            "phase6_release_target_invalid", "发布目标逻辑 ID 越过受控目录。"
        )
    return target


def _safe_record(record: AgentRunRecord) -> dict[str, Any]:
    return {
        "status": record.status,
        "provider": record.provider,
        "transport": record.transport,
        "model": record.model,
        "completion_integrity": record.completion_integrity,
        "completion_error_code": record.completion_error_code,
        "final_output": _safe_text(record.final_output),
        "tool_calls": [safe_audit_value(call.to_dict()) for call in record.tool_calls],
        "usage": record.usage.to_dict(),
        "latency_ms": record.latency_ms,
        "cost_usd": record.cost_usd,
        "approval_interruptions": [
            safe_audit_value(item.to_dict())
            for item in record.approval_interruptions
        ],
        "tracing_disabled": record.tracing_disabled,
        "model_responses": [item.to_dict() for item in record.model_responses],
        "tool_observations": [
            safe_audit_value(item.to_dict()) for item in record.tool_observations
        ],
    }


def _safe_text(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = safe_audit_value(value)
    if not isinstance(cleaned, str):
        return "[OUTPUT_OMITTED]"
    cleaned = _PATH_TRAVERSAL.sub("[PATH_REDACTED]", cleaned)
    cleaned = _WINDOWS_ABSOLUTE_PATH.sub("[PATH_REDACTED]", cleaned)
    cleaned = _UNIX_ABSOLUTE_PATH.sub("[PATH_REDACTED]", cleaned)
    return cleaned[:8192] + ("[TRUNCATED]" if len(cleaned) > 8192 else "")


def _optional_text_sha256(value: str | None) -> str | None:
    return hashlib.sha256(value.encode("utf-8")).hexdigest() if value is not None else None


def _safe_error_code(exc: Exception) -> str:
    code = getattr(exc, "code", None)
    if isinstance(code, str) and re.fullmatch(r"[a-z0-9_]{1,96}", code):
        return code
    return "phase6_harness_" + type(exc).__name__.lower()


def _is_execution_failure(exc: Exception) -> bool:
    if isinstance(exc, Phase6AgentError):
        return True
    return isinstance(exc, Phase6RunError) and exc.code in {
        "phase6_agent_record_invalid",
        "phase6_agent_runner_failed",
        "phase6_case_timeout",
    }


def _pricing_manifest(
    prices: tuple[float, float] | None,
    *,
    provider: str,
    deepseek_policy: _DeepSeekCnyPolicy | None = None,
) -> dict[str, Any]:
    if deepseek_policy is not None:
        return {
            "status": "operator_snapshot_bound_policy",
            "provider": provider,
            "currency": "CNY",
            "snapshot_date": deepseek_policy.pricing_snapshot_date,
            "source_url": deepseek_policy.pricing_source_url,
            "input_cache_miss_per_million_cny": _money_text(
                _DEEPSEEK_INPUT_CACHE_MISS_CNY_PER_MILLION
            ),
            "output_per_million_cny": _money_text(
                _DEEPSEEK_OUTPUT_CNY_PER_MILLION
            ),
            "estimate_method": "peak_hours_all_input_cache_miss",
            "cached_input_discount_applied": False,
            "pricing_snapshot_fetched_by_runner": False,
            "cost_is_estimate": True,
            "formula": "reported_input_tokens*3/1e6 + reported_output_tokens*9/1e6",
        }
    if prices is None:
        return {
            "status": "not_provided",
            "provider": provider,
            "input_per_million_usd": None,
            "output_per_million_usd": None,
            "cost_is_estimate": False,
        }
    return {
        "status": "provided_by_operator",
        "provider": provider,
        "input_per_million_usd": prices[0],
        "output_per_million_usd": prices[1],
        "cost_is_estimate": True,
        "formula": "reported_input_tokens*input_rate + reported_output_tokens*output_rate",
        "cached_input_discount_applied": False,
    }


def _deepseek_cny_policy_report(
    policy: _DeepSeekCnyPolicy | None,
    state: Mapping[str, Any],
    *,
    attempted_case_count: int,
) -> dict[str, Any]:
    if policy is None:
        return {
            "status": "not_provided",
            "provider": None,
            "currency": None,
        }
    observed_cases = int(state["usage_observed_cases"])
    usage_complete = (
        state["usage_complete"] is True
        and observed_cases == attempted_case_count
    )
    if attempted_case_count == 0:
        coverage_status = "not_run"
    elif usage_complete:
        coverage_status = "complete"
    elif observed_cases:
        coverage_status = "partial"
    else:
        coverage_status = "unavailable"
    observed_cost = state["conservative_estimated_cost_cny"]
    return {
        "status": "enabled",
        "provider": "deepseek",
        "currency": "CNY",
        "local_observed_cost_stop_cny": _money_text(
            policy.local_observed_cost_stop_cny
        ),
        "provider_billing_hard_cap": False,
        "local_hard_stop_only": True,
        "strict_provider_hard_cap": False,
        "enforcement_semantics": "pre_case_reserve_plus_post_case_observed_stop",
        "single_case_overshoot_possible": True,
        "pricing": _pricing_manifest(
            None, provider="deepseek", deepseek_policy=policy
        ),
        "limits": {
            "input_tokens": policy.total_input_tokens_cap,
            "output_tokens": policy.total_output_tokens_cap,
            "requests": policy.total_requests_cap,
            "total_timeout_seconds": policy.total_timeout_seconds,
        },
        "per_case_start_reserve": {
            "input_tokens": _DEEPSEEK_CASE_INPUT_TOKEN_RESERVE,
            "output_tokens": _DEEPSEEK_CASE_OUTPUT_TOKEN_RESERVE,
            "requests": "max_turns",
            "cost_cny": _money_text(_DEEPSEEK_CASE_COST_RESERVE_CNY),
        },
        "execution_policy": {
            "sequential": True,
            "client_retries": 0,
            "resume": False,
            "pre_case_reserve_guard": True,
            "post_case_hard_stop": True,
            "missing_usage_stops_before_next_case": True,
        },
        "coverage": {
            "status": coverage_status,
            "attempted_cases": attempted_case_count,
            "usage_observed_cases": observed_cases,
            "usage_missing_cases": int(state["usage_missing_cases"]),
            "usage_and_cost_coverage": (
                observed_cases / attempted_case_count
                if attempted_case_count
                else None
            ),
        },
        "observed_usage": {
            "known_completed_requests": int(state["requests"]),
            "total_request_count_status": (
                "complete" if usage_complete else "unknown_lower_bound_only"
            ),
            "input_tokens": int(state["input_tokens"]),
            "output_tokens": int(state["output_tokens"]),
            "conservative_observed_cost_cny": _money_text(observed_cost),
        },
        "total_estimated_cost_cny": (
            _money_text(observed_cost) if usage_complete and attempted_case_count else None
        ),
        "observed_cost_lower_bound_cny": _money_text(observed_cost),
        "actual_provider_bill_cny": None,
        "cost_interpretation": (
            "complete_peak_all_cache_miss_estimate"
            if usage_complete and attempted_case_count
            else "lower_bound_from_complete_reported_cases_total_cost_unknown"
        ),
    }


def _apply_deepseek_cost_report(
    report: dict[str, Any],
    *,
    policy: _DeepSeekCnyPolicy | None,
    state: Mapping[str, Any],
    attempted_case_count: int,
) -> None:
    if policy is None:
        return
    policy_report = report["deepseek_cny_policy"]
    coverage = policy_report["coverage"]
    cost = report["cost"]
    cost.update(
        {
            "status": coverage["status"],
            "currency": "CNY",
            "total_usd": None,
            "total_cny": policy_report["total_estimated_cost_cny"],
            "observed_lower_bound_cny": policy_report[
                "observed_cost_lower_bound_cny"
            ],
            "coverage": coverage["usage_and_cost_coverage"],
            "attempted_runs": attempted_case_count,
            "priced_runs": coverage["usage_observed_cases"],
            "actual_provider_bill": None,
        }
    )


def _task_category(task: Phase6Task) -> str:
    categories = sorted(set(task.tags).intersection(_CATEGORY_TAGS))
    if len(categories) == 1:
        return categories[0]
    return "uncategorized" if not categories else "multiple"


def _grouped_execution_report(
    *,
    selected: Sequence[Phase6Task],
    result_rows: Sequence[Mapping[str, Any]],
    scores: Sequence[Phase6TaskScore],
) -> dict[str, Any]:
    score_by_task = {score.task_id: score for score in scores}
    row_by_task = {str(row["task_id"]): row for row in result_rows}

    def group(tasks: Sequence[Phase6Task]) -> dict[str, Any]:
        planned_ids = [task.task_id for task in tasks]
        attempted_ids = [task_id for task_id in planned_ids if task_id in row_by_task]
        completed_ids = [
            task_id
            for task_id in attempted_ids
            if row_by_task[task_id].get("execution_status") == "executed"
        ]
        passed = sum(
            score_by_task[task_id].task_pass is True
            for task_id in attempted_ids
            if task_id in score_by_task
        )
        attempted_latencies = [
            score_by_task[task_id].latency_ms
            for task_id in attempted_ids
            if task_id in score_by_task
            and score_by_task[task_id].latency_ms is not None
        ]
        completed_latencies = [
            score_by_task[task_id].latency_ms
            for task_id in completed_ids
            if task_id in score_by_task
            and score_by_task[task_id].latency_ms is not None
        ]
        failed_attempted = len(attempted_ids) - passed
        return {
            "planned": len(planned_ids),
            "attempted": len(attempted_ids),
            "completed": len(completed_ids),
            "passed": passed,
            "failed_attempted": failed_attempted,
            "not_started": len(planned_ids) - len(attempted_ids),
            "attempted_failure_rate": (
                failed_attempted / len(attempted_ids) if attempted_ids else None
            ),
            "planned_nonpass_rate": (
                (len(planned_ids) - passed) / len(planned_ids)
                if planned_ids
                else None
            ),
            "latency_ms": {
                "attempted": _latency_statistics(attempted_latencies),
                "completed": _latency_statistics(completed_latencies),
            },
        }

    tags = sorted({tag for task in selected for tag in task.tags})
    categories = sorted({_task_category(task) for task in selected})
    return {
        "overall": group(selected),
        "by_category": {
            category: group(
                [task for task in selected if _task_category(task) == category]
            )
            for category in categories
        },
        "by_tag": {
            tag: group([task for task in selected if tag in task.tags])
            for tag in tags
        },
        "tag_groups_overlap": True,
    }


def _latency_statistics(values: Sequence[float | None]) -> dict[str, Any]:
    clean = sorted(
        float(value)
        for value in values
        if value is not None and math.isfinite(float(value)) and float(value) >= 0
    )
    if not clean:
        return {
            "count": 0,
            "min": None,
            "mean": None,
            "p50_nearest_rank": None,
            "p95_nearest_rank": None,
            "max": None,
        }

    def nearest_rank(fraction: float) -> float:
        index = max(math.ceil(fraction * len(clean)) - 1, 0)
        return clean[index]

    return {
        "count": len(clean),
        "min": clean[0],
        "mean": sum(clean) / len(clean),
        "p50_nearest_rank": nearest_rank(0.50),
        "p95_nearest_rank": nearest_rank(0.95),
        "max": clean[-1],
    }


def _summary_markdown(report: Mapping[str, Any]) -> str:
    success = report.get("overall_success_rate")
    success_text = "unavailable" if success is None else f"{success:.1%}"
    latency = report.get("latency_ms", {})
    cost = report.get("cost", {})
    usage = report.get("usage", {})
    denominators = report.get("failure_denominators", {})
    deepseek_policy = report.get("deepseek_cny_policy", {})
    cost_value = (
        cost.get("total_cny")
        if cost.get("currency") == "CNY"
        else cost.get("total_usd")
    )
    cost_currency = cost.get("currency") or "USD"
    deepseek_budget_line = ""
    if deepseek_policy.get("status") == "enabled":
        deepseek_budget_line = (
            "- DeepSeek 本地观测成本 stop："
            f"{deepseek_policy.get('local_observed_cost_stop_cny')} "
            "CNY；这是 pre-case reserve + post-case observed stop，不是 "
            "Provider 账户硬上限。\n"
        )
    depth60_binding = report.get("depth60_plan_binding")
    depth60_line = ""
    if isinstance(depth60_binding, Mapping):
        depth60_line = (
            f"- Depth-60 plan：`{depth60_binding.get('plan_id')}` / "
            f"`{depth60_binding.get('plan_commitment_sha256')}`\n"
        )
    return (
        "# Phase 6 在线 Agent 评测\n\n"
        "> 范围：OpenAI Agents SDK 驱动的 provider 模型规划、工具轨迹、"
        "最终证据回答与审批中断。\n\n"
        f"- Provider：`{report.get('provider')}`\n"
        f"- Transport：`{report.get('transport')}`\n"
        f"- 模型：`{report.get('model')}`\n"
        f"- 单次响应输出上限：{report.get('max_output_tokens')} tokens\n"
        f"- Split：`{report.get('selected_split')}`\n"
        f"{depth60_line}"
        f"- Run status：`{report.get('run_status')}`；stop reason："
        f"`{report.get('stop_reason')}`\n"
        f"- 计划/尝试/完成/未开始：{report.get('selected_case_count')}/"
        f"{report.get('attempted_case_count')}/"
        f"{report.get('completed_case_count')}/"
        f"{report.get('not_started_case_count')}\n"
        f"- 成功率：{success_text}\n"
        f"- 通过：{report.get('passed')}/{report.get('included')}\n"
        f"- 失败分母：attempted {denominators.get('failed_attempted_cases')}/"
        f"{denominators.get('attempted_cases')}；planned non-pass "
        f"{denominators.get('planned_case_nonpass_count')}/"
        f"{denominators.get('planned_cases')}\n"
        f"- Agent 执行失败：{report.get('execution_failure_count')}；"
        f"Harness 错误：{report.get('harness_error_count')}\n"
        f"- 逻辑工具错误率：{report.get('logical_tool_error_rate')}\n"
        f"- 本地工具 attempt 错误率：{report.get('local_tool_attempt_error_rate')}\n"
        f"- 回答完整性准确率/覆盖率：{report.get('completion_integrity_accuracy')}/"
        f"{report.get('completion_integrity_coverage')}；失败数："
        f"{len(report.get('completion_failures', []))}\n"
        f"- Evidence 标签完整性准确率："
        f"{report.get('evidence_label_integrity_accuracy')}\n"
        f"- Numeric CLAIM 任务准确率：{report.get('numeric_claim_task_accuracy')}\n"
        f"- Evidence precision：{report.get('evidence_precision')}\n"
        f"- 延迟 P50/P95（ms）：{latency.get('p50_nearest_rank')}/"
        f"{latency.get('p95_nearest_rank')}\n"
        f"- Usage：`{usage.get('status')}`；coverage={usage.get('coverage')}；"
        f"requests/input/output={usage.get('requests')}/"
        f"{usage.get('input_tokens')}/{usage.get('output_tokens')}\n"
        f"- 成本状态：`{cost.get('status')}`；保守估算："
        f"{cost_value} {cost_currency}；实际 Provider 账单：unknown\n"
        f"{deepseek_budget_line}"
        "\n"
        "本报告只描述冻结的仓库可见 development 任务，不能证明 private holdout、"
        "未知生产集泛化或生产 SLA。\n"
    )


def _write_json(path: Path, payload: Any) -> None:
    path.write_text(
        json.dumps(
            payload,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
            allow_nan=False,
        )
        + "\n",
        encoding="utf-8",
    )


def _write_jsonl(path: Path, payloads: Sequence[Mapping[str, Any]]) -> None:
    path.write_text(
        "".join(
            json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
            + "\n"
            for payload in payloads
        ),
        encoding="utf-8",
    )


def _source_tree_sha256(source_root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(source_root.glob("*.py"), key=lambda item: item.name):
        digest.update(path.name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
