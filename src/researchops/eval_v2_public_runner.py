from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import secrets
import time
from contextlib import AbstractContextManager
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_UP
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from .eval_v2_contracts import EvalV2ContractError
from .artifact_security import ArtifactPermissionError, enable_parent_acl_inheritance
from .eval_v2_dataset_prep import EvalV2LogicalDatasetRegistry
from .eval_v2_freeze import (
    load_public_regression_task_orders,
    validate_public_regression_candidate,
)
from .eval_v2_inspect_backend import EvalV2InspectDatasetBackend
from .eval_v2_provider_executor import EvalV2ProviderExecutor
from .eval_v2_public import (
    EvalV2PublicTask,
    load_eval_v2_dataset_manifest,
    load_eval_v2_public_tasks,
)
from .eval_v2_runner import (
    EvalV2ExecutorResult,
    EvalV2Observation,
    EvalV2TaskExecutor,
    EvalV2ToolGateway,
    _validate_completion_failure_metadata,
    _validate_completion_telemetry,
    _validate_tool_telemetry,
    score_eval_v2_observation,
    summarize_completion_telemetry,
)
from .model_providers import get_provider


PUBLIC_REGRESSION_RUNNER_VERSION = "1.1"
PUBLIC_REGRESSION_RUN_SCHEMA_VERSION = "1.1"
_PROVIDER_CHANNEL = "provider_behavior"
_FAULT_CHANNEL = "deterministic_fault_injection"
_CHANNELS = (_FAULT_CHANNEL, _PROVIDER_CHANNEL)
_EXPECTED_PROVIDER_TASKS = 31
_EXPECTED_FAULT_TASKS = 9
_EXPECTED_REPETITIONS = 3
_EXPECTED_PROVIDER_CASES = _EXPECTED_PROVIDER_TASKS * _EXPECTED_REPETITIONS
_EXPECTED_FAULT_CASES = _EXPECTED_FAULT_TASKS * _EXPECTED_REPETITIONS

# DeepSeek V4 Flash public pricing checked on 2026-08-21.  The guard deliberately
# uses the higher peak-hours price and treats every input token as a cache miss;
# time-of-day and cache discounts are never used to justify another paid case.
_PRICING_SOURCE_URL = "https://api-docs.deepseek.com/zh-cn/quick_start/pricing/"
_PRICING_VERIFIED_AT = "2026-08-21"
_INPUT_CACHE_MISS_CNY_PER_MILLION = Decimal("3")
_OUTPUT_CNY_PER_MILLION = Decimal("9")
_DEFAULT_MAX_TOTAL_INPUT_TOKENS = 1_000_000
_DEFAULT_MAX_TOTAL_OUTPUT_TOKENS = 333_333
_DEFAULT_MAX_MODEL_CALLS = 744

# One case can make at most eight model turns under the locked candidate.  The
# reserves are deliberately far above observed controlled-prompt/tool usage and
# must remain available before another case starts.  They reduce overshoot risk
# but are not a provider-side account spending cap or a mathematical bill limit.
_CASE_INPUT_TOKEN_RESERVE = 250_000
_CASE_OUTPUT_TOKEN_RESERVE = 16_000
_CASE_COST_RESERVE_CNY = Decimal("4.00")
_MONEY_QUANTUM = Decimal("0.000001")
_EMPTY_CASE_CHAIN_SHA256 = "0" * 64
_FORBIDDEN_PERSISTED_KEYS = frozenset(
    {
        "api_key",
        "authorization",
        "credentials",
        "direct_identifiers",
        "final_output",
        "incomplete_details",
        "prompt",
        "provider_output_body",
        "provider_response_body",
        "provider_status_raw_value",
        "raw_output",
        "raw_response",
        "response_body",
        "tool_result",
    }
)


ProgressCallback = Callable[[Mapping[str, Any]], None]


class DeterministicFaultExecutor:
    """Local fault fixture; its results must never be attributed to a model."""

    def __init__(self, scenario: str) -> None:
        if scenario not in {
            "provider_timeout",
            "output_truncation",
            "side_effect_outcome_unknown",
        }:
            raise EvalV2ContractError(
                "eval_v2_fault_scenario_invalid",
                "Deterministic fault executor 收到未登记场景。",
            )
        self._scenario = scenario

    def execute(
        self,
        public_input: Mapping[str, Any],
        gateway: EvalV2ToolGateway,
    ) -> EvalV2ExecutorResult:
        context = public_input.get("context")
        if not isinstance(context, Mapping):
            raise EvalV2ContractError(
                "eval_v2_fault_context_invalid", "Fault fixture 缺少授权 context。"
            )
        if self._scenario == "output_truncation":
            dataset_id = context.get("dataset_id")
            if not isinstance(dataset_id, str):
                raise EvalV2ContractError(
                    "eval_v2_fault_context_invalid", "Fault fixture 缺少 dataset_id。"
                )
            gateway.call("inspect_dataset", {"dataset_id": dataset_id})
            error_code = "output_limit_suspected"
            completion_status = "output_truncated"
            completion_failure_source = "output_limit_suspected"
        elif self._scenario == "side_effect_outcome_unknown":
            bundle_id = context.get("bundle_id")
            release_name = context.get("release_name")
            if not isinstance(bundle_id, str) or not isinstance(release_name, str):
                raise EvalV2ContractError(
                    "eval_v2_fault_context_invalid", "Fault fixture 缺少发布 scope。"
                )
            gateway.call(
                "publish_aggregate_results",
                {"bundle_id": bundle_id, "release_name": release_name},
            )
            error_code = "outcome_unknown"
            completion_status = "outcome_unknown"
            completion_failure_source = None
        else:
            error_code = "provider_timeout"
            completion_status = "provider_timeout"
            completion_failure_source = None
        return EvalV2ExecutorResult(
            outcome="controlled_failure",
            final_output="",
            approval_state="not_required",
            safety_violation=False,
            side_effect_occurred=False,
            error_code=error_code,
            completion_status=completion_status,
            completion_failure_source=completion_failure_source,
            model_call_count=0,
            input_tokens=None,
            output_tokens=None,
            provider_id="deterministic_fault_injection",
            model_id="fault-fixture-v1",
            transport_id="local_scripted",
        )


def conservative_cost_cny(input_tokens: int, output_tokens: int) -> Decimal:
    """Return the peak-price, all-cache-miss estimate, rounded upward."""

    for value, label in (
        (input_tokens, "input_tokens"),
        (output_tokens, "output_tokens"),
    ):
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise EvalV2ContractError(
                "eval_v2_budget_usage_invalid", f"{label} 必须是非负整数。"
            )
    cost = (
        Decimal(input_tokens) * _INPUT_CACHE_MISS_CNY_PER_MILLION
        + Decimal(output_tokens) * _OUTPUT_CNY_PER_MILLION
    ) / Decimal(1_000_000)
    return cost.quantize(_MONEY_QUANTUM, rounding=ROUND_UP)


def run_public_regression_online(
    *,
    project_root: str | Path,
    candidate_path: str | Path,
    registry_path: str | Path,
    output_directory: str | Path,
    api_key: str,
    budget_cny: Decimal | str | float,
    confirm_online: bool,
    resume: bool = False,
    max_total_input_tokens: int = _DEFAULT_MAX_TOTAL_INPUT_TOKENS,
    max_total_output_tokens: int = _DEFAULT_MAX_TOTAL_OUTPUT_TOKENS,
    max_model_calls: int = _DEFAULT_MAX_MODEL_CALLS,
    progress_callback: ProgressCallback | None = None,
) -> dict[str, Any]:
    """Run the locked public-regression candidate with atomic sanitized checkpoints."""

    root = Path(project_root).resolve()
    candidate_source = Path(candidate_path).resolve()
    registry_source = Path(registry_path).resolve()
    output = _validate_output_directory(root, Path(output_directory))
    budget = _validate_budget(budget_cny)
    limits = _validate_limits(
        max_total_input_tokens,
        max_total_output_tokens,
        max_model_calls,
    )
    if not confirm_online:
        raise EvalV2ContractError(
            "eval_v2_online_confirmation_required",
            "Public-regression 在线运行需要 --confirm-online。",
        )
    if not isinstance(api_key, str) or not api_key.strip():
        raise EvalV2ContractError(
            "eval_v2_provider_key_missing",
            "未配置 candidate Provider 的 API key；未创建在线请求。",
        )

    freeze = validate_public_regression_candidate(
        project_root=root,
        candidate_path=candidate_source,
        verify_environment=True,
    )
    candidate = _load_json_object(candidate_source, "candidate")
    provider_config = candidate["provider_config"]
    execution_policy = candidate["execution_policy"]
    if not isinstance(provider_config, Mapping) or not isinstance(
        execution_policy, Mapping
    ):
        raise EvalV2ContractError(
            "eval_v2_public_candidate_invalid", "Candidate runtime 配置无效。"
        )
    provider = get_provider(str(provider_config["provider_id"]))
    model_id = provider.validate_model(str(provider_config["model_id"]))
    if (
        provider.provider_id != "deepseek"
        or provider.transport_id != provider_config["transport_id"]
        or int(execution_policy["repetitions_per_provider"])
        != _EXPECTED_REPETITIONS
    ):
        raise EvalV2ContractError(
            "eval_v2_public_candidate_provider_invalid",
            "Candidate provider 身份或重复次数发生漂移。",
        )

    dataset_manifest = load_eval_v2_dataset_manifest(
        root / "evals" / "v2" / "external_datasets.json"
    )
    all_tasks = load_eval_v2_public_tasks(
        root / "evals" / "v2" / "public_tasks.jsonl", dataset_manifest
    )
    public_tasks = {
        task.task_id: task for task in all_tasks if task.split == "public_regression"
    }
    split_source = root / "evals" / "v2" / "public_regression_split_manifest.json"
    orders = {
        channel: load_public_regression_task_orders(
            split_source, execution_channel=channel
        )
        for channel in _CHANNELS
    }
    _validate_execution_plan(public_tasks, orders)
    artifacts_root = (root / "artifacts").resolve()
    if (
        not registry_source.is_file()
        or not registry_source.is_relative_to(artifacts_root)
    ):
        raise EvalV2ContractError(
            "eval_v2_registry_path_not_allowed",
            "Public-regression registry 必须是项目 artifacts 下的文件。",
        )
    registry = EvalV2LogicalDatasetRegistry.load(registry_source)
    component_hashes = candidate.get("component_hashes")
    if (
        not isinstance(component_hashes, Mapping)
        or registry.dataset_manifest_sha256
        != component_hashes.get("dataset_manifest_sha256")
    ):
        raise EvalV2ContractError(
            "eval_v2_registry_manifest_mismatch",
            "Registry 未绑定到 candidate 锁定的 dataset manifest。",
        )
    inspect_backend = EvalV2InspectDatasetBackend(registry)
    expected_registry_ids = set(dataset_manifest.by_id())
    observed_registry_ids = {
        item["dataset_id"] for item in inspect_backend.public_catalog()
    }
    if observed_registry_ids != expected_registry_ids:
        raise EvalV2ContractError(
            "eval_v2_registry_scope_mismatch",
            "Registry 数据集 scope 与锁定 dataset manifest 不一致。",
        )

    shared_run_root = artifacts_root / "eval_v2_public_regression"
    _prepare_shared_run_root(shared_run_root)
    commitment = str(freeze["candidate_commitment_sha256"])
    receipt_path = shared_run_root / f"{commitment}.receipt.json"
    with _ExclusiveRunLock(shared_run_root / f".{commitment}.lock"):
        _validate_existing_candidate_receipt(
            receipt_path=receipt_path,
            root=root,
            output=output,
            freeze=freeze,
            resume=resume,
        )
        preflight = _provider_preflight_descriptor(provider.provider_id, model_id)
        if not (resume and output.exists()):
            preflight = _verify_provider_model_access(
                provider=provider,
                model_id=model_id,
                api_key=api_key,
                timeout_seconds=min(
                    20.0, float(provider_config["case_timeout_seconds"])
                ),
            )
        state_spec = _state_spec(
            freeze=freeze,
            candidate=candidate,
            provider_config=provider_config,
            provider_preflight=preflight,
            orders=orders,
            budget=budget,
            limits=limits,
            registry_manifest_sha256=registry.dataset_manifest_sha256,
            registry_file_sha256=_sha256_file(registry_source),
        )
        output_created = _prepare_output_container(output, resume=resume)
        with _ExclusiveRunLock(output / ".public_regression.lock"):
            return _run_locked_public_regression(
                root=root,
                output=output,
                output_created=output_created,
                resume=resume,
                state_spec=state_spec,
                receipt_path=receipt_path,
                public_tasks=public_tasks,
                orders=orders,
                inspect_backend=inspect_backend,
                provider=provider,
                model_id=model_id,
                provider_config=provider_config,
                api_key=api_key,
                progress_callback=progress_callback,
            )


def _run_locked_public_regression(
    *,
    root: Path,
    output: Path,
    output_created: bool,
    resume: bool,
    state_spec: Mapping[str, Any],
    receipt_path: Path,
    public_tasks: Mapping[str, EvalV2PublicTask],
    orders: Mapping[str, Mapping[int, Sequence[str]]],
    inspect_backend: EvalV2InspectDatasetBackend,
    provider: Any,
    model_id: str,
    provider_config: Mapping[str, Any],
    api_key: str,
    progress_callback: ProgressCallback | None,
) -> dict[str, Any]:
    state_path = output / "public_regression_state.json"
    state = _initialize_or_resume_state(
        output=output,
        state_path=state_path,
        state_spec=state_spec,
        resume=resume,
        output_created=output_created,
    )
    _bind_candidate_receipt(
        receipt_path=receipt_path,
        root=root,
        output=output,
        state=state,
    )
    if state["status"] in {"complete", "stopped"}:
        return _finalize_run(root, output, state)

    completed_keys = {entry["case_key"] for entry in state["completed_cases"]}
    for repetition_index in range(1, _EXPECTED_REPETITIONS + 1):
        for task_id in orders[_FAULT_CHANNEL][repetition_index]:
            case_key = _case_key(_FAULT_CHANNEL, repetition_index, task_id)
            if case_key in completed_keys:
                continue
            task = public_tasks[task_id]
            entry = _execute_and_checkpoint_case(
                state=state,
                state_path=state_path,
                channel=_FAULT_CHANNEL,
                repetition_index=repetition_index,
                task=task,
                executor=DeterministicFaultExecutor(task.scenario),
                inspect_backend=inspect_backend,
            )
            completed_keys.add(entry["case_key"])
            _emit_progress(progress_callback, state, entry)

    fault_entries = [
        entry
        for entry in state["completed_cases"]
        if entry["channel"] == _FAULT_CHANNEL
    ]
    if len(fault_entries) != _EXPECTED_FAULT_CASES or not all(
        entry["score"]["passed"] for entry in fault_entries
    ):
        state["status"] = "stopped"
        state["stop_reason"] = "fault_harness_failed"
        state["updated_at_utc"] = _utc_now()
        _atomic_write_json(state_path, state)
        return _finalize_run(root, output, state)

    provider_executor = EvalV2ProviderExecutor(
        provider=provider,
        model_id=model_id,
        api_key=api_key,
        confirm_online=True,
        max_turns=int(provider_config["max_turns"]),
        run_timeout_seconds=float(provider_config["case_timeout_seconds"]),
        tracing_disabled=True,
        bilingual_output=False,
        max_output_tokens=int(provider_config["normal_max_output_tokens"]),
    )
    for repetition_index in range(1, _EXPECTED_REPETITIONS + 1):
        for task_id in orders[_PROVIDER_CHANNEL][repetition_index]:
            case_key = _case_key(_PROVIDER_CHANNEL, repetition_index, task_id)
            if case_key in completed_keys:
                continue
            stop_reason = _provider_start_guard(state)
            if stop_reason is not None:
                state["status"] = "stopped"
                state["stop_reason"] = stop_reason
                state["updated_at_utc"] = _utc_now()
                _atomic_write_json(state_path, state)
                return _finalize_run(root, output, state)
            entry = _execute_and_checkpoint_case(
                state=state,
                state_path=state_path,
                channel=_PROVIDER_CHANNEL,
                repetition_index=repetition_index,
                task=public_tasks[task_id],
                executor=provider_executor,
                inspect_backend=inspect_backend,
            )
            completed_keys.add(entry["case_key"])
            _emit_progress(progress_callback, state, entry)
            stop_reason = _provider_post_case_guard(state, entry)
            if stop_reason is not None:
                state["status"] = "stopped"
                state["stop_reason"] = stop_reason
                state["updated_at_utc"] = _utc_now()
                _atomic_write_json(state_path, state)
                return _finalize_run(root, output, state)

    if (
        len(
            [
                entry
                for entry in state["completed_cases"]
                if entry["channel"] == _PROVIDER_CHANNEL
            ]
        )
        != _EXPECTED_PROVIDER_CASES
    ):
        raise EvalV2ContractError(
            "eval_v2_public_run_incomplete", "Public-regression 完成计数不匹配。"
        )
    state["status"] = "complete"
    state["stop_reason"] = None
    state["completed_at_utc"] = _utc_now()
    state["updated_at_utc"] = state["completed_at_utc"]
    _atomic_write_json(state_path, state)
    return _finalize_run(root, output, state)


def _execute_and_checkpoint_case(
    *,
    state: dict[str, Any],
    state_path: Path,
    channel: str,
    repetition_index: int,
    task: EvalV2PublicTask,
    executor: EvalV2TaskExecutor,
    inspect_backend: EvalV2InspectDatasetBackend,
) -> dict[str, Any]:
    case_key = _case_key(channel, repetition_index, task.task_id)
    state["in_progress_case"] = {
        "case_key": case_key,
        "channel": channel,
        "repetition_index": repetition_index,
        "task_id": task.task_id,
        "marked_at_utc": _utc_now(),
    }
    state["updated_at_utc"] = state["in_progress_case"]["marked_at_utc"]
    _atomic_write_json(state_path, state)

    gateway = EvalV2ToolGateway(task, inspect_backend)
    started = time.perf_counter()
    result = executor.execute(task.public_input(), gateway)
    latency_ms = (time.perf_counter() - started) * 1000.0
    if not isinstance(result, EvalV2ExecutorResult):
        raise EvalV2ContractError(
            "eval_v2_executor_result_invalid",
            "Public-regression executor 必须返回 EvalV2ExecutorResult。",
        )
    _validate_tool_telemetry(result)
    _validate_completion_telemetry(result)
    observation = EvalV2Observation(
        task_id=task.task_id,
        outcome=result.outcome,
        final_output=result.final_output,
        tool_calls=gateway.tool_calls,
        approval_state=result.approval_state,
        safety_violation=result.safety_violation,
        side_effect_occurred=result.side_effect_occurred,
        error_code=result.error_code,
        completion_status=result.completion_status,
        latency_ms=latency_ms,
        model_call_count=result.model_call_count,
        input_tokens=result.input_tokens,
        output_tokens=result.output_tokens,
        provider_id=result.provider_id,
        model_id=result.model_id,
        transport_id=result.transport_id,
        model_requested_tool_call_count=result.model_requested_tool_call_count,
        model_requested_tool_call_count_source=(
            result.model_requested_tool_call_count_source
        ),
        deduplicated_tool_call_count=result.deduplicated_tool_call_count,
        gateway_dispatched_tool_call_count=len(gateway.tool_calls),
        backend_executed_tool_call_count=gateway.backend_executed_tool_call_count,
        completion_failure_source=result.completion_failure_source,
    )
    score = score_eval_v2_observation(task, observation)
    entry = {
        "case_key": case_key,
        "channel": channel,
        "repetition_index": repetition_index,
        "task_id": task.task_id,
        "score": score.to_dict(),
        "diagnostics": {
            "outcome": observation.outcome,
            "approval_state": observation.approval_state,
            "safety_violation": observation.safety_violation,
            "side_effect_occurred": observation.side_effect_occurred,
            "error_code": observation.error_code,
            "completion_status": observation.completion_status,
            "completion_failure_source": observation.completion_failure_source,
            "observed_tool_sequence": [
                call.tool_name for call in observation.tool_calls
            ],
            "observed_tool_statuses": [call.status for call in observation.tool_calls],
            "latency_ms": round(observation.latency_ms, 3),
            "provider": {
                "provider_id": observation.provider_id,
                "model_id": observation.model_id,
                "transport_id": observation.transport_id,
            },
            "usage": {
                "model_call_count": observation.model_call_count,
                "input_tokens": observation.input_tokens,
                "output_tokens": observation.output_tokens,
            },
            "tool_call_telemetry": {
                "model_requested_tool_call_count": (
                    observation.model_requested_tool_call_count
                ),
                "model_requested_tool_call_count_source": (
                    observation.model_requested_tool_call_count_source
                ),
                "deduplicated_tool_call_count": (
                    observation.deduplicated_tool_call_count
                ),
                "gateway_dispatched_tool_call_count": (
                    observation.gateway_dispatched_tool_call_count
                ),
                "backend_executed_tool_call_count": (
                    observation.backend_executed_tool_call_count
                ),
            },
        },
        "completed_at_utc": _utc_now(),
    }
    entry["previous_case_record_sha256"] = state["case_chain_head_sha256"]
    entry["case_record_sha256"] = _case_record_sha256(entry)
    if channel == _PROVIDER_CHANNEL:
        _apply_provider_usage(state, entry)
    state["completed_cases"].append(entry)
    state["case_chain_head_sha256"] = entry["case_record_sha256"]
    state["in_progress_case"] = None
    state["updated_at_utc"] = entry["completed_at_utc"]
    _atomic_write_json(state_path, state)
    return entry


def _apply_provider_usage(state: dict[str, Any], entry: Mapping[str, Any]) -> None:
    usage = entry["diagnostics"]["usage"]
    calls = usage["model_call_count"]
    input_tokens = usage["input_tokens"]
    output_tokens = usage["output_tokens"]
    if (
        isinstance(calls, bool)
        or not isinstance(calls, int)
        or calls < 1
        or isinstance(input_tokens, bool)
        or not isinstance(input_tokens, int)
        or input_tokens < 0
        or isinstance(output_tokens, bool)
        or not isinstance(output_tokens, int)
        or output_tokens < 0
    ):
        state["budget"]["usage_complete"] = False
        return
    totals = state["budget"]["observed_usage"]
    totals["model_call_count"] += calls
    totals["input_tokens"] += input_tokens
    totals["output_tokens"] += output_tokens
    totals["conservative_estimated_cost_cny"] = _money_text(
        conservative_cost_cny(totals["input_tokens"], totals["output_tokens"])
    )


def _provider_start_guard(state: Mapping[str, Any]) -> str | None:
    budget = state["budget"]
    if budget["usage_complete"] is not True:
        return "provider_usage_unavailable"
    usage = budget["observed_usage"]
    limits = budget["limits"]
    current_cost = Decimal(usage["conservative_estimated_cost_cny"])
    authorized = Decimal(budget["authorized_budget_cny"])
    if current_cost + _CASE_COST_RESERVE_CNY > authorized:
        return "budget_reserve_exhausted"
    if usage["input_tokens"] + _CASE_INPUT_TOKEN_RESERVE > limits["input_tokens"]:
        return "input_token_reserve_exhausted"
    if usage["output_tokens"] + _CASE_OUTPUT_TOKEN_RESERVE > limits["output_tokens"]:
        return "output_token_reserve_exhausted"
    max_turns = int(state["provider"]["max_turns"])
    if usage["model_call_count"] + max_turns > limits["model_calls"]:
        return "model_call_reserve_exhausted"
    return None


def _provider_post_case_guard(
    state: Mapping[str, Any], entry: Mapping[str, Any]
) -> str | None:
    if state["budget"]["usage_complete"] is not True:
        return "provider_usage_unavailable"
    usage = state["budget"]["observed_usage"]
    limits = state["budget"]["limits"]
    if entry["diagnostics"]["usage"]["model_call_count"] > state["provider"]["max_turns"]:
        return "per_case_model_call_limit_exceeded"
    if usage["model_call_count"] > limits["model_calls"]:
        return "model_call_limit_exceeded"
    if usage["input_tokens"] > limits["input_tokens"]:
        return "input_token_limit_exceeded"
    if usage["output_tokens"] > limits["output_tokens"]:
        return "output_token_limit_exceeded"
    if Decimal(usage["conservative_estimated_cost_cny"]) > Decimal(
        state["budget"]["authorized_budget_cny"]
    ):
        return "authorized_budget_exceeded"
    return None


def _state_spec(
    *,
    freeze: Mapping[str, Any],
    candidate: Mapping[str, Any],
    provider_config: Mapping[str, Any],
    provider_preflight: Mapping[str, Any],
    orders: Mapping[str, Mapping[int, Sequence[str]]],
    budget: Decimal,
    limits: Mapping[str, int],
    registry_manifest_sha256: str,
    registry_file_sha256: str,
) -> dict[str, Any]:
    return {
        "schema_version": PUBLIC_REGRESSION_RUN_SCHEMA_VERSION,
        "runner_version": PUBLIC_REGRESSION_RUNNER_VERSION,
        "candidate": {
            "candidate_id": freeze["candidate_id"],
            "candidate_commitment_sha256": freeze[
                "candidate_commitment_sha256"
            ],
            "campaign_status": candidate["campaign_status_expected"],
            "full_campaign_frozen": False,
            "private_holdout_access_authorized": False,
            "model_quality_claim_allowed": False,
        },
        "provider": {
            "provider_id": provider_config["provider_id"],
            "model_id": provider_config["model_id"],
            "transport_id": provider_config["transport_id"],
            "max_turns": provider_config["max_turns"],
            "case_timeout_seconds": provider_config["case_timeout_seconds"],
            "normal_max_output_tokens": provider_config[
                "normal_max_output_tokens"
            ],
            "refusal_max_output_tokens": provider_config[
                "refusal_max_output_tokens"
            ],
            "clarification_max_output_tokens": provider_config[
                "clarification_max_output_tokens"
            ],
        },
        "provider_preflight": dict(provider_preflight),
        "registry_binding": {
            "dataset_manifest_sha256": registry_manifest_sha256,
            "registry_file_sha256": registry_file_sha256,
            "dataset_count": 3,
            "model_access": "aggregate_tools_only",
        },
        "execution_plan": {
            "repetitions": _EXPECTED_REPETITIONS,
            "provider_behavior_cases": _EXPECTED_PROVIDER_CASES,
            "deterministic_fault_injection_cases": _EXPECTED_FAULT_CASES,
            "fault_results_attributed_to_model": False,
            "channels_reported_separately": True,
            "orders": {
                channel: {
                    str(repetition): list(channel_orders[repetition])
                    for repetition in range(1, _EXPECTED_REPETITIONS + 1)
                }
                for channel, channel_orders in orders.items()
            },
        },
        "budget": {
            "currency": "CNY",
            "authorized_budget_cny": _money_text(budget),
            "estimate_method": "conservative_peak_all_input_cache_miss",
            "provider_billing_hard_cap": False,
            "pricing": {
                "source_url": _PRICING_SOURCE_URL,
                "verified_at": _PRICING_VERIFIED_AT,
                "time_band_basis": "peak_hours_worst_case",
                "peak_hours_beijing": "09:00-12:00,14:00-18:00",
                "input_cache_miss_cny_per_million_tokens": _money_text(
                    _INPUT_CACHE_MISS_CNY_PER_MILLION
                ),
                "output_cny_per_million_tokens": _money_text(
                    _OUTPUT_CNY_PER_MILLION
                ),
            },
            "limits": dict(limits),
            "per_case_start_reserve": {
                "input_tokens": _CASE_INPUT_TOKEN_RESERVE,
                "output_tokens": _CASE_OUTPUT_TOKEN_RESERVE,
                "cost_cny": _money_text(_CASE_COST_RESERVE_CNY),
            },
        },
    }


def _initialize_or_resume_state(
    *,
    output: Path,
    state_path: Path,
    state_spec: Mapping[str, Any],
    resume: bool,
    output_created: bool,
) -> dict[str, Any]:
    if not output_created:
        if not resume:
            raise EvalV2ContractError(
                "eval_v2_output_exists",
                "Public-regression 输出目录已存在；不会覆盖。使用 --resume 继续。",
            )
        if not output.is_dir() or not state_path.is_file():
            raise EvalV2ContractError(
                "eval_v2_public_resume_invalid", "Resume 目录缺少有效 checkpoint。"
            )
        state = _load_json_object(state_path, "public regression checkpoint")
        _assert_sanitized(state)
        for key in (
            "schema_version",
            "runner_version",
            "candidate",
            "provider",
            "provider_preflight",
            "registry_binding",
            "execution_plan",
        ):
            if state.get(key) != state_spec[key]:
                raise EvalV2ContractError(
                    "eval_v2_public_resume_drift",
                    f"Resume checkpoint 的 {key} 与当前锁定运行不一致。",
                )
        expected_budget = state_spec["budget"]
        observed_budget = state.get("budget")
        if not isinstance(observed_budget, Mapping):
            raise EvalV2ContractError(
                "eval_v2_public_resume_invalid", "Resume checkpoint budget 无效。"
            )
        for key in (
            "currency",
            "authorized_budget_cny",
            "estimate_method",
            "provider_billing_hard_cap",
            "pricing",
            "limits",
            "per_case_start_reserve",
        ):
            if observed_budget.get(key) != expected_budget[key]:
                raise EvalV2ContractError(
                    "eval_v2_public_resume_drift",
                    "Resume checkpoint 的预算或价格配置发生漂移。",
                )
        if state.get("in_progress_case") is not None:
            raise EvalV2ContractError(
                "eval_v2_public_resume_ambiguous_inflight",
                "Checkpoint 含未确认的 in-flight case；不会自动重跑并重复计费。",
            )
        if state.get("status") not in {"in_progress", "complete", "stopped"}:
            raise EvalV2ContractError(
                "eval_v2_public_resume_invalid", "Resume checkpoint status 无效。"
            )
        if not isinstance(state.get("completed_cases"), list):
            raise EvalV2ContractError(
                "eval_v2_public_resume_invalid", "Resume completed_cases 无效。"
            )
        if state.get("artifact_policy") != {
            "model_outputs_persisted": False,
            "api_keys_persisted": False,
            "row_level_data_persisted": False,
            "atomic_checkpoint": True,
        }:
            raise EvalV2ContractError(
                "eval_v2_public_resume_invalid", "Resume artifact policy 无效。"
            )
        case_keys = [entry.get("case_key") for entry in state["completed_cases"]]
        if len(case_keys) != len(set(case_keys)) or not all(
            isinstance(value, str) for value in case_keys
        ):
            raise EvalV2ContractError(
                "eval_v2_public_resume_invalid", "Resume case key 重复或无效。"
            )
        _validate_resume_completed_cases(state)
        return state

    if not output_created or resume:
        raise EvalV2ContractError(
            "eval_v2_public_resume_missing", "--resume 指定的输出目录不存在。"
        )
    now = _utc_now()
    budget_state = dict(state_spec["budget"])
    budget_state["usage_complete"] = True
    budget_state["observed_usage"] = {
        "model_call_count": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "conservative_estimated_cost_cny": _money_text(Decimal("0")),
    }
    state = {
        **{key: value for key, value in state_spec.items() if key != "budget"},
        "run_id": "PUBREG-" + secrets.token_hex(8).upper(),
        "status": "in_progress",
        "started_at_utc": now,
        "updated_at_utc": now,
        "completed_at_utc": None,
        "stop_reason": None,
        "budget": budget_state,
        "in_progress_case": None,
        "case_chain_head_sha256": _EMPTY_CASE_CHAIN_SHA256,
        "completed_cases": [],
        "artifact_policy": {
            "model_outputs_persisted": False,
            "api_keys_persisted": False,
            "row_level_data_persisted": False,
            "atomic_checkpoint": True,
        },
    }
    _atomic_write_json(state_path, state)
    return state


def _finalize_run(root: Path, output: Path, state: Mapping[str, Any]) -> dict[str, Any]:
    report = _build_report(state)
    summary = _render_summary(report)
    state_path = output / "public_regression_state.json"
    report_path = output / "public_regression_report.json"
    summary_path = output / "public_regression_summary.md"
    _atomic_write_json(report_path, report)
    _atomic_write_text(summary_path, summary)
    manifest = {
        "schema_version": "1.0",
        "run_id": state["run_id"],
        "candidate_commitment_sha256": state["candidate"][
            "candidate_commitment_sha256"
        ],
        "candidate_execution_receipt_sha256": _sha256_file(
            root
            / "artifacts"
            / "eval_v2_public_regression"
            / (
                state["candidate"]["candidate_commitment_sha256"]
                + ".receipt.json"
            )
        ),
        "case_chain_head_sha256": state["case_chain_head_sha256"],
        "model_outputs_persisted": False,
        "api_keys_persisted": False,
        "files": {
            state_path.name: _sha256_file(state_path),
            report_path.name: _sha256_file(report_path),
            summary_path.name: _sha256_file(summary_path),
        },
    }
    _atomic_write_json(output / "artifact_manifest.json", manifest)
    return _result_pointer(root, output, state)


def _build_report(state: Mapping[str, Any]) -> dict[str, Any]:
    completed = state["completed_cases"]
    provider_entries = [
        entry for entry in completed if entry["channel"] == _PROVIDER_CHANNEL
    ]
    fault_entries = [entry for entry in completed if entry["channel"] == _FAULT_CHANNEL]
    budget_report = dict(state["budget"])
    budget_report["reported_usage_cost_is_complete"] = (
        state["budget"]["usage_complete"] is True
    )
    budget_report["cost_interpretation"] = (
        "conservative_upper_estimate_for_all_provider_cases"
        if state["budget"]["usage_complete"] is True
        else "lower_bound_from_reported_usage_only_total_cost_unknown"
    )
    return {
        "schema_version": PUBLIC_REGRESSION_RUN_SCHEMA_VERSION,
        "runner_version": PUBLIC_REGRESSION_RUNNER_VERSION,
        "run_id": state["run_id"],
        "status": state["status"],
        "stop_reason": state["stop_reason"],
        "case_chain_head_sha256": state["case_chain_head_sha256"],
        "single_use_candidate_receipt": True,
        "started_at_utc": state["started_at_utc"],
        "completed_at_utc": state["completed_at_utc"],
        "candidate": state["candidate"],
        "provider": state["provider"],
        "provider_preflight": state["provider_preflight"],
        "registry_binding": state["registry_binding"],
        "evidence_status": "public_regression_candidate_run",
        "model_quality_claim_allowed": False,
        "unknown_production_generalization_claim_allowed": False,
        "budget": budget_report,
        "provider_behavior": _channel_report(
            provider_entries,
            expected_tasks=_EXPECTED_PROVIDER_TASKS,
            expected_cases=_EXPECTED_PROVIDER_CASES,
            results_attributed_to_locked_candidate_system=True,
        ),
        "deterministic_fault_injection": _channel_report(
            fault_entries,
            expected_tasks=_EXPECTED_FAULT_TASKS,
            expected_cases=_EXPECTED_FAULT_CASES,
            results_attributed_to_locked_candidate_system=False,
        ),
        "cross_channel_model_success_rate": None,
        "artifact_policy": state["artifact_policy"],
        "limitations": [
            "Provider-behavior results describe this locked public run only; they do not establish unknown production-set generalization.",
            "Deterministic fault-injection results validate local control/scorer paths and are not attributed to the model.",
            "The cost estimate is derived from reported token usage and is not a provider-side billing hard cap; if usage is missing, the displayed amount is only a lower bound.",
            "The full Eval v2 campaign remains design_only; private holdout access is not authorized.",
            "No model response body, API key, or row-level dataset content is persisted.",
            "The local case hash chain and single-use receipt are not an external digital signature.",
        ],
    }


def _channel_report(
    entries: Sequence[Mapping[str, Any]],
    *,
    expected_tasks: int,
    expected_cases: int,
    results_attributed_to_locked_candidate_system: bool,
) -> dict[str, Any]:
    repetitions = []
    for repetition_index in range(1, _EXPECTED_REPETITIONS + 1):
        selected = [
            entry
            for entry in entries
            if entry["repetition_index"] == repetition_index
        ]
        repetitions.append(_repetition_report(repetition_index, selected))
    passed = sum(bool(entry["score"]["passed"]) for entry in entries)
    task_ids = sorted({entry["task_id"] for entry in entries})
    by_task = {
        task_id: [entry for entry in entries if entry["task_id"] == task_id]
        for task_id in task_ids
    }
    all_repeat_passed = sum(
        len(task_entries) == _EXPECTED_REPETITIONS
        and all(entry["score"]["passed"] for entry in task_entries)
        for task_entries in by_task.values()
    )
    any_failed = sum(
        any(not entry["score"]["passed"] for entry in task_entries)
        for task_entries in by_task.values()
    )
    incomplete_with_results = sum(
        len(task_entries) < _EXPECTED_REPETITIONS
        for task_entries in by_task.values()
    )
    return {
        "results_attributed_to_model_alone": False,
        "results_attributed_to_locked_candidate_system": (
            results_attributed_to_locked_candidate_system
        ),
        "expected_task_count_per_repetition": expected_tasks,
        "expected_case_count": expected_cases,
        "completed_case_count": len(entries),
        "passed_case_count": passed,
        "failed_case_count": len(entries) - passed,
        "case_success_rate": passed / len(entries) if entries else None,
        "task_count_with_any_result": len(task_ids),
        "tasks_passing_all_three_repetitions": all_repeat_passed,
        "tasks_with_any_failed_repetition": any_failed,
        "tasks_incomplete_with_partial_results": incomplete_with_results,
        "tasks_not_started": expected_tasks - len(task_ids),
        "completion_telemetry": _summarize_public_completion_telemetry(entries),
        "repetitions": repetitions,
    }


def _repetition_report(
    repetition_index: int, entries: Sequence[Mapping[str, Any]]
) -> dict[str, Any]:
    scores = [entry["score"] for entry in entries]
    passed = sum(bool(score["passed"]) for score in scores)
    metric_names = (
        "outcome_match",
        "tool_sequence_match",
        "tool_arguments_match",
        "required_phrases_match",
        "forbidden_assertions_match",
        "evidence_match",
        "numeric_claims_match",
        "approval_match",
        "safety_match",
        "completion_match",
    )
    latencies = [float(entry["diagnostics"]["latency_ms"]) for entry in entries]
    usages = [entry["diagnostics"]["usage"] for entry in entries]
    usage_complete = all(
        isinstance(item["model_call_count"], int)
        and (
            item["model_call_count"] == 0
            or (
                isinstance(item["input_tokens"], int)
                and isinstance(item["output_tokens"], int)
            )
        )
        for item in usages
    )
    return {
        "repetition_index": repetition_index,
        "task_order": [entry["task_id"] for entry in entries],
        "task_order_sha256": _task_order_sha256(
            [entry["task_id"] for entry in entries]
        )
        if entries
        else None,
        "task_count": len(entries),
        "passed": passed,
        "failed": len(entries) - passed,
        "success_rate": passed / len(entries) if entries else None,
        "component_accuracy": {
            name: sum(bool(score[name]) for score in scores) / len(scores)
            if scores
            else None
            for name in metric_names
        },
        "p50_latency_ms": _percentile(latencies, 0.50) if latencies else None,
        "p95_latency_ms": _percentile(latencies, 0.95) if latencies else None,
        "usage_complete": usage_complete,
        "usage": {
            "model_call_count": sum(item["model_call_count"] for item in usages),
            "input_tokens": sum(item["input_tokens"] for item in usages)
            if usage_complete and all(item["input_tokens"] is not None for item in usages)
            else None,
            "output_tokens": sum(item["output_tokens"] for item in usages)
            if usage_complete and all(item["output_tokens"] is not None for item in usages)
            else None,
        },
        "completion_telemetry": _summarize_public_completion_telemetry(entries),
        "task_scores": [entry["score"] for entry in entries],
        "task_diagnostics": {
            entry["task_id"]: entry["diagnostics"] for entry in entries
        },
    }


def _summarize_public_completion_telemetry(
    entries: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    records: list[dict[str, Any]] = []
    for entry in entries:
        diagnostics = entry.get("diagnostics")
        if not isinstance(diagnostics, Mapping):
            raise EvalV2ContractError(
                "eval_v2_completion_telemetry_invalid",
                "Public-regression case diagnostics 无效。",
            )
        _validate_completion_failure_metadata(
            outcome=diagnostics.get("outcome"),
            error_code=diagnostics.get("error_code"),
            completion_status=diagnostics.get("completion_status"),
            completion_failure_source=diagnostics.get(
                "completion_failure_source"
            ),
        )
        records.append(
            {
                "error_code": diagnostics.get("error_code"),
                "completion_failure_source": diagnostics.get(
                    "completion_failure_source"
                ),
            }
        )
    return {
        **summarize_completion_telemetry(records),
        "case_count": len(entries),
    }


def _render_summary(report: Mapping[str, Any]) -> str:
    provider = report["provider_behavior"]
    fault = report["deterministic_fault_injection"]
    usage = report["budget"]["observed_usage"]
    cost_label = (
        "Conservative estimated cost"
        if report["budget"]["reported_usage_cost_is_complete"]
        else "Reported-usage cost lower bound (total unknown)"
    )
    provider_rate = provider["case_success_rate"]
    fault_rate = fault["case_success_rate"]
    provider_completion = provider["completion_telemetry"]
    fault_completion = fault["completion_telemetry"]
    return "\n".join(
        [
            "# Eval v2 Public-Regression Run",
            "",
            f"- Status: `{report['status']}`",
            f"- Run ID: `{report['run_id']}`",
            f"- Candidate: `{report['candidate']['candidate_id']}`",
            f"- Candidate commitment: `{report['candidate']['candidate_commitment_sha256']}`",
            f"- Provider: `{report['provider']['provider_id']}/{report['provider']['model_id']}`",
            "",
            "## Budget",
            "",
            f"- Authorized budget: CNY {report['budget']['authorized_budget_cny']}",
            f"- {cost_label}: CNY {usage['conservative_estimated_cost_cny']}",
            f"- Reported usage: {usage['input_tokens']} input tokens, {usage['output_tokens']} output tokens, {usage['model_call_count']} model requests",
            "- Cost method: peak-hours prices with all input tokens treated as cache misses; this is not a provider billing hard cap.",
            "",
            "## Provider-behavior channel",
            "",
            f"- Completed: {provider['completed_case_count']}/{provider['expected_case_count']}",
            f"- Passed: {provider['passed_case_count']}",
            f"- Case success rate: {_rate_text(provider_rate)}",
            (
                "- Completion telemetry: "
                f"{provider_completion['classified_failure_count']}/"
                f"{provider_completion['eligible_failure_count']} classified; "
                f"status `{provider_completion['coverage_status']}`"
            ),
            f"- Tasks passing all three repetitions: {provider['tasks_passing_all_three_repetitions']}/{provider['expected_task_count_per_repetition']}",
            "",
            "## Deterministic fault-injection channel",
            "",
            f"- Completed: {fault['completed_case_count']}/{fault['expected_case_count']}",
            f"- Passed: {fault['passed_case_count']}",
            f"- Harness success rate: {_rate_text(fault_rate)}",
            (
                "- Completion telemetry: "
                f"{fault_completion['classified_failure_count']}/"
                f"{fault_completion['eligible_failure_count']} classified; "
                f"status `{fault_completion['coverage_status']}`"
            ),
            "- These local fixture results are not attributed to the model.",
            "",
            "## Claim boundary",
            "",
            "This public candidate run does not establish private-holdout or unknown production-set generalization. The full Eval v2 campaign remains design-only, and no cross-channel model success rate is reported.",
            "",
        ]
    )


def _validate_execution_plan(
    public_tasks: Mapping[str, EvalV2PublicTask],
    orders: Mapping[str, Mapping[int, Sequence[str]]],
) -> None:
    if len(public_tasks) != _EXPECTED_PROVIDER_TASKS + _EXPECTED_FAULT_TASKS:
        raise EvalV2ContractError(
            "eval_v2_public_plan_invalid", "Public-regression task 数量不是锁定的 40。"
        )
    expected_counts = {
        _PROVIDER_CHANNEL: _EXPECTED_PROVIDER_TASKS,
        _FAULT_CHANNEL: _EXPECTED_FAULT_TASKS,
    }
    channel_sets: dict[str, set[str]] = {}
    for channel, expected_count in expected_counts.items():
        channel_orders = orders.get(channel)
        if channel_orders is None or set(channel_orders) != {1, 2, 3}:
            raise EvalV2ContractError(
                "eval_v2_public_plan_invalid", "Execution channel 重复顺序无效。"
            )
        sets = [set(channel_orders[index]) for index in (1, 2, 3)]
        if (
            any(len(channel_orders[index]) != expected_count for index in (1, 2, 3))
            or not all(item == sets[0] for item in sets[1:])
            or any(len(channel_orders[index]) != len(set(channel_orders[index])) for index in (1, 2, 3))
        ):
            raise EvalV2ContractError(
                "eval_v2_public_plan_invalid", "Execution channel scope 或顺序无效。"
            )
        channel_sets[channel] = sets[0]
    if (
        channel_sets[_PROVIDER_CHANNEL] & channel_sets[_FAULT_CHANNEL]
        or channel_sets[_PROVIDER_CHANNEL] | channel_sets[_FAULT_CHANNEL]
        != set(public_tasks)
    ):
        raise EvalV2ContractError(
            "eval_v2_public_plan_invalid", "Execution channel 未形成互斥完整分区。"
        )
    fault_scenarios = {
        "provider_timeout",
        "output_truncation",
        "side_effect_outcome_unknown",
    }
    if any(
        public_tasks[task_id].scenario in fault_scenarios
        for task_id in channel_sets[_PROVIDER_CHANNEL]
    ) or any(
        public_tasks[task_id].scenario not in fault_scenarios
        for task_id in channel_sets[_FAULT_CHANNEL]
    ):
        raise EvalV2ContractError(
            "eval_v2_public_plan_invalid", "Fault 场景与执行通道不一致。"
        )


def _validate_output_directory(project_root: Path, output_directory: Path) -> Path:
    artifacts_root = (project_root / "artifacts").resolve()
    resolved = output_directory.resolve()
    if resolved == artifacts_root or not resolved.is_relative_to(artifacts_root):
        raise EvalV2ContractError(
            "eval_v2_output_path_not_allowed",
            "Public-regression 产物必须位于项目 artifacts 的独立子目录。",
        )
    return resolved


def _prepare_shared_run_root(path: Path) -> None:
    try:
        path.mkdir(parents=True, exist_ok=True)
    except FileExistsError as exc:
        raise EvalV2ContractError(
            "eval_v2_public_receipt_root_invalid",
            "Public-regression receipt root 必须是目录。",
        ) from exc
    if not path.is_dir():
        raise EvalV2ContractError(
            "eval_v2_public_receipt_root_invalid",
            "Public-regression receipt root 必须是目录。",
        )
    try:
        enable_parent_acl_inheritance(path)
    except ArtifactPermissionError as exc:
        raise EvalV2ContractError(
            "eval_v2_artifact_permission_failed",
            "无法恢复 Public-regression receipt 目录的父级 ACL 继承。",
        ) from exc


def _provider_preflight_descriptor(provider_id: str, model_id: str) -> dict[str, Any]:
    return {
        "status": "verified",
        "provider_id": provider_id,
        "model_id": model_id,
        "verification_method": "provider_models_list",
        "network_calls": 1,
        "model_token_calls": 0,
    }


def _verify_provider_model_access(
    *,
    provider: Any,
    model_id: str,
    api_key: str,
    timeout_seconds: float,
) -> dict[str, Any]:
    if getattr(provider, "provider_id", None) != "deepseek":
        raise EvalV2ContractError(
            "eval_v2_provider_preflight_provider_invalid",
            "Public-regression model preflight 只允许锁定的 DeepSeek Provider。",
        )
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(
            _verify_provider_model_access_async(
                provider=provider,
                model_id=model_id,
                api_key=api_key,
                timeout_seconds=timeout_seconds,
            )
        )
    raise EvalV2ContractError(
        "eval_v2_provider_preflight_loop_active",
        "Provider model catalog 预检不能在活动 event loop 中运行。",
    )


async def _verify_provider_model_access_async(
    *,
    provider: Any,
    model_id: str,
    api_key: str,
    timeout_seconds: float,
) -> dict[str, Any]:
    if getattr(provider, "provider_id", None) != "deepseek":
        raise EvalV2ContractError(
            "eval_v2_provider_preflight_provider_invalid",
            "Public-regression model preflight 只允许锁定的 DeepSeek Provider。",
        )
    try:
        from openai import AsyncOpenAI
    except ImportError as exc:
        raise EvalV2ContractError(
            "eval_v2_provider_preflight_unavailable", "未安装 Provider client。"
        ) from exc
    client_arguments: dict[str, Any] = {
        "api_key": api_key,
        "timeout": timeout_seconds,
        "max_retries": 0,
    }
    base_url = getattr(provider, "base_url", None)
    if isinstance(base_url, str) and base_url:
        client_arguments["base_url"] = base_url
    client = AsyncOpenAI(**client_arguments)
    try:
        page = await client.models.list()
    except Exception as exc:
        status_code = getattr(exc, "status_code", None)
        if status_code in {401, 403}:
            code = "eval_v2_provider_preflight_auth_failed"
            message = "Provider API Key 认证失败；未创建正式运行。"
        elif status_code == 429:
            code = "eval_v2_provider_preflight_rate_limited"
            message = "Provider 模型目录暂时限流；未创建正式运行。"
        elif isinstance(status_code, int) and status_code >= 500:
            code = "eval_v2_provider_preflight_unavailable"
            message = "Provider 模型目录暂时不可用；未创建正式运行。"
        elif "timeout" in type(exc).__name__.lower():
            code = "eval_v2_provider_preflight_timeout"
            message = "Provider 模型目录预检超时；未创建正式运行。"
        else:
            code = "eval_v2_provider_preflight_failed"
            message = "无法核验 Provider/model；未创建正式运行。"
        raise EvalV2ContractError(code, message) from exc
    finally:
        await client.close()
    model_ids = {
        item.id
        for item in getattr(page, "data", ())
        if isinstance(getattr(item, "id", None), str)
    }
    if model_id not in model_ids:
        raise EvalV2ContractError(
            "eval_v2_provider_model_unavailable",
            "Candidate model 未出现在该 API Key 可用的模型目录中。",
        )
    return _provider_preflight_descriptor(provider.provider_id, model_id)


def _validate_existing_candidate_receipt(
    *,
    receipt_path: Path,
    root: Path,
    output: Path,
    freeze: Mapping[str, Any],
    resume: bool,
) -> None:
    if not receipt_path.exists():
        return
    receipt = _load_json_object(receipt_path, "candidate execution receipt")
    _assert_sanitized(receipt)
    expected_output = output.relative_to(root).as_posix()
    if (
        receipt.get("schema_version") != "1.0"
        or receipt.get("candidate_id") != freeze["candidate_id"]
        or receipt.get("candidate_commitment_sha256")
        != freeze["candidate_commitment_sha256"]
        or receipt.get("artifact_directory") != expected_output
        or receipt.get("single_use_candidate") is not True
    ):
        raise EvalV2ContractError(
            "eval_v2_public_candidate_receipt_mismatch",
            "Candidate execution receipt 与请求不匹配；不会启动。",
        )
    if not resume:
        raise EvalV2ContractError(
            "eval_v2_public_candidate_already_started",
            "该 candidate 已绑定一次正式运行；只能对原目录使用 --resume。",
        )


def _bind_candidate_receipt(
    *,
    receipt_path: Path,
    root: Path,
    output: Path,
    state: Mapping[str, Any],
) -> None:
    payload = {
        "schema_version": "1.0",
        "candidate_id": state["candidate"]["candidate_id"],
        "candidate_commitment_sha256": state["candidate"][
            "candidate_commitment_sha256"
        ],
        "run_id": state["run_id"],
        "artifact_directory": output.relative_to(root).as_posix(),
        "single_use_candidate": True,
        "created_at_utc": state["started_at_utc"],
        "model_outputs_persisted": False,
        "api_keys_persisted": False,
    }
    if receipt_path.exists():
        if _load_json_object(receipt_path, "candidate execution receipt") != payload:
            raise EvalV2ContractError(
                "eval_v2_public_candidate_receipt_mismatch",
                "Candidate execution receipt 的 run binding 不一致。",
            )
        return
    _atomic_write_json(receipt_path, payload)


def _prepare_output_container(output: Path, *, resume: bool) -> bool:
    if output.exists():
        if not output.is_dir():
            raise EvalV2ContractError(
                "eval_v2_public_output_invalid", "Public-regression 输出必须是目录。"
            )
        return False
    if resume:
        raise EvalV2ContractError(
            "eval_v2_public_resume_missing", "--resume 指定的输出目录不存在。"
        )
    output.parent.mkdir(parents=True, exist_ok=True)
    try:
        output.mkdir()
    except FileExistsError:
        return False
    try:
        enable_parent_acl_inheritance(output)
    except ArtifactPermissionError as exc:
        raise EvalV2ContractError(
            "eval_v2_artifact_permission_failed",
            "无法恢复 Public-regression 产物目录的父级 ACL 继承。",
        ) from exc
    return True


class _ExclusiveRunLock(AbstractContextManager["_ExclusiveRunLock"]):
    """Process-wide advisory lock; the lock file contains no run data."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._handle: Any | None = None

    def __enter__(self) -> "_ExclusiveRunLock":
        self._handle = self._path.open("a+b")
        self._handle.seek(0, os.SEEK_END)
        if self._handle.tell() == 0:
            self._handle.write(b"0")
            self._handle.flush()
        self._handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(self._handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(self._handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (OSError, BlockingIOError) as exc:
            self._handle.close()
            self._handle = None
            raise EvalV2ContractError(
                "eval_v2_public_run_locked",
                "另一个进程正在使用该 Public-regression 输出目录。",
            ) from exc
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        del exc_type, exc, traceback
        if self._handle is None:
            return
        try:
            self._handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(self._handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
        finally:
            self._handle.close()
            self._handle = None


def _validate_resume_completed_cases(state: Mapping[str, Any]) -> None:
    plan_orders = state["execution_plan"]["orders"]
    expected_keys: list[str] = []
    for channel in _CHANNELS:
        for repetition_index in range(1, _EXPECTED_REPETITIONS + 1):
            for task_id in plan_orders[channel][str(repetition_index)]:
                expected_keys.append(_case_key(channel, repetition_index, task_id))
    completed = state["completed_cases"]
    if not all(isinstance(entry, Mapping) for entry in completed):
        raise EvalV2ContractError(
            "eval_v2_public_resume_invalid", "Resume case entry 无效。"
        )
    observed_keys = [entry.get("case_key") for entry in completed]
    if not all(isinstance(case_key, str) for case_key in observed_keys):
        raise EvalV2ContractError(
            "eval_v2_public_resume_invalid", "Resume case key 无效。"
        )
    if observed_keys != expected_keys[: len(observed_keys)]:
        raise EvalV2ContractError(
            "eval_v2_public_resume_invalid",
            "Resume completed case 不是预承诺分通道顺序的完整前缀。",
        )
    provider_entries: list[Mapping[str, Any]] = []
    case_chain_head = _EMPTY_CASE_CHAIN_SHA256
    for entry in completed:
        parts = entry["case_key"].split(":", 2)
        try:
            parsed_repetition = int(parts[1]) if len(parts) == 3 else None
        except ValueError:
            parsed_repetition = None
        if (
            len(parts) != 3
            or entry.get("channel") != parts[0]
            or entry.get("repetition_index") != parsed_repetition
            or entry.get("task_id") != parts[2]
            or not isinstance(entry.get("score"), Mapping)
            or entry["score"].get("task_id") != parts[2]
            or not isinstance(entry.get("diagnostics"), Mapping)
        ):
            raise EvalV2ContractError(
                "eval_v2_public_resume_invalid", "Resume case entry 内容不一致。"
            )
        observed_provider = entry["diagnostics"].get("provider")
        expected_provider = (
            {
                "provider_id": state["provider"]["provider_id"],
                "model_id": state["provider"]["model_id"],
                "transport_id": state["provider"]["transport_id"],
            }
            if entry["channel"] == _PROVIDER_CHANNEL
            else {
                "provider_id": "deterministic_fault_injection",
                "model_id": "fault-fixture-v1",
                "transport_id": "local_scripted",
            }
        )
        if observed_provider != expected_provider:
            raise EvalV2ContractError(
                "eval_v2_public_resume_invalid", "Resume provider identity 不一致。"
            )
        if (
            entry.get("previous_case_record_sha256") != case_chain_head
            or entry.get("case_record_sha256") != _case_record_sha256(entry)
        ):
            raise EvalV2ContractError(
                "eval_v2_public_resume_case_chain_invalid",
                "Resume case hash chain 无效。",
            )
        case_chain_head = entry["case_record_sha256"]
        diagnostics = entry["diagnostics"]
        _validate_completion_failure_metadata(
            outcome=diagnostics.get("outcome"),
            error_code=diagnostics.get("error_code"),
            completion_status=diagnostics.get("completion_status"),
            completion_failure_source=diagnostics.get(
                "completion_failure_source"
            ),
        )
        if entry["channel"] == _PROVIDER_CHANNEL:
            provider_entries.append(entry)

    if state.get("case_chain_head_sha256") != case_chain_head:
        raise EvalV2ContractError(
            "eval_v2_public_resume_case_chain_invalid",
            "Resume case chain head 不一致。",
        )

    expected_usage_complete = True
    calls = input_tokens = output_tokens = 0
    for entry in provider_entries:
        usage = entry["diagnostics"].get("usage")
        if not isinstance(usage, Mapping):
            expected_usage_complete = False
            continue
        case_calls = usage.get("model_call_count")
        case_input = usage.get("input_tokens")
        case_output = usage.get("output_tokens")
        if (
            isinstance(case_calls, bool)
            or not isinstance(case_calls, int)
            or case_calls < 1
            or isinstance(case_input, bool)
            or not isinstance(case_input, int)
            or case_input < 0
            or isinstance(case_output, bool)
            or not isinstance(case_output, int)
            or case_output < 0
        ):
            expected_usage_complete = False
            continue
        calls += case_calls
        input_tokens += case_input
        output_tokens += case_output
    expected_usage = {
        "model_call_count": calls,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "conservative_estimated_cost_cny": _money_text(
            conservative_cost_cny(input_tokens, output_tokens)
        ),
    }
    budget = state["budget"]
    if (
        budget.get("usage_complete") is not expected_usage_complete
        or budget.get("observed_usage") != expected_usage
    ):
        raise EvalV2ContractError(
            "eval_v2_public_resume_invalid", "Resume 累计 usage 与 case 记录不一致。"
        )
    if state["status"] == "complete" and len(completed) != len(expected_keys):
        raise EvalV2ContractError(
            "eval_v2_public_resume_invalid", "Complete checkpoint 的 case 数量不完整。"
        )


def _validate_budget(value: Decimal | str | float) -> Decimal:
    try:
        budget = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise EvalV2ContractError(
            "eval_v2_budget_invalid", "budget_cny 必须是有限正数。"
        ) from exc
    if not budget.is_finite() or budget <= 0:
        raise EvalV2ContractError(
            "eval_v2_budget_invalid", "budget_cny 必须是有限正数。"
        )
    return budget.quantize(_MONEY_QUANTUM, rounding=ROUND_UP)


def _validate_limits(
    max_total_input_tokens: int,
    max_total_output_tokens: int,
    max_model_calls: int,
) -> dict[str, int]:
    values = {
        "input_tokens": max_total_input_tokens,
        "output_tokens": max_total_output_tokens,
        "model_calls": max_model_calls,
    }
    if any(
        isinstance(value, bool) or not isinstance(value, int) or value < 1
        for value in values.values()
    ):
        raise EvalV2ContractError(
            "eval_v2_budget_limit_invalid", "Token/model-call 上限必须是正整数。"
        )
    return values


def _load_json_object(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise EvalV2ContractError(
            "eval_v2_file_unreadable", f"无法读取 {label}。"
        ) from exc
    if not isinstance(value, dict):
        raise EvalV2ContractError(
            "eval_v2_public_run_invalid", f"{label} 必须是 JSON object。"
        )
    return value


def _atomic_write_json(path: Path, payload: Mapping[str, Any]) -> None:
    _assert_sanitized(payload)
    serialized = json.dumps(
        payload,
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
        allow_nan=False,
    ) + "\n"
    _atomic_write_text(path, serialized)


def _atomic_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
    try:
        with temporary.open("x", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _assert_sanitized(value: Any) -> None:
    if isinstance(value, Mapping):
        forbidden = set(value).intersection(_FORBIDDEN_PERSISTED_KEYS)
        if forbidden:
            raise EvalV2ContractError(
                "eval_v2_public_artifact_unsafe",
                "Artifact 含禁止持久化字段。",
            )
        for item in value.values():
            _assert_sanitized(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _assert_sanitized(item)
    elif isinstance(value, str) and (
        value.startswith("sk-") and len(value) >= 20
    ):
        raise EvalV2ContractError(
            "eval_v2_public_artifact_unsafe", "Artifact 疑似包含凭据。"
        )


def _case_key(channel: str, repetition_index: int, task_id: str) -> str:
    return f"{channel}:{repetition_index}:{task_id}"


def _case_record_sha256(entry: Mapping[str, Any]) -> str:
    payload = dict(entry)
    payload.pop("case_record_sha256", None)
    serialized = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(serialized).hexdigest()


def _task_order_sha256(task_ids: Sequence[str]) -> str:
    return hashlib.sha256("\n".join(task_ids).encode("utf-8")).hexdigest()


def _percentile(values: Sequence[float], quantile: float) -> float:
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * quantile
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    fraction = position - lower
    return ordered[lower] + (ordered[upper] - ordered[lower]) * fraction


def _money_text(value: Decimal) -> str:
    return format(value.quantize(_MONEY_QUANTUM, rounding=ROUND_UP), "f")


def _rate_text(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.2%}"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _emit_progress(
    callback: ProgressCallback | None,
    state: Mapping[str, Any],
    entry: Mapping[str, Any],
) -> None:
    if callback is None:
        return
    provider_completed = sum(
        item["channel"] == _PROVIDER_CHANNEL for item in state["completed_cases"]
    )
    fault_completed = sum(
        item["channel"] == _FAULT_CHANNEL for item in state["completed_cases"]
    )
    callback(
        {
            "event": "case_completed",
            "channel": entry["channel"],
            "repetition_index": entry["repetition_index"],
            "task_id": entry["task_id"],
            "passed": entry["score"]["passed"],
            "provider_cases_completed": provider_completed,
            "provider_cases_expected": _EXPECTED_PROVIDER_CASES,
            "fault_cases_completed": fault_completed,
            "fault_cases_expected": _EXPECTED_FAULT_CASES,
            "conservative_estimated_cost_cny": state["budget"]["observed_usage"][
                "conservative_estimated_cost_cny"
            ],
            "usage_complete": state["budget"]["usage_complete"],
        }
    )


def _result_pointer(root: Path, output: Path, state: Mapping[str, Any]) -> dict[str, Any]:
    provider_completed = sum(
        entry["channel"] == _PROVIDER_CHANNEL for entry in state["completed_cases"]
    )
    fault_completed = sum(
        entry["channel"] == _FAULT_CHANNEL for entry in state["completed_cases"]
    )
    return {
        "status": state["status"],
        "run_id": state["run_id"],
        "stop_reason": state["stop_reason"],
        "provider_cases_completed": provider_completed,
        "provider_cases_expected": _EXPECTED_PROVIDER_CASES,
        "fault_cases_completed": fault_completed,
        "fault_cases_expected": _EXPECTED_FAULT_CASES,
        "conservative_estimated_cost_cny": state["budget"]["observed_usage"][
            "conservative_estimated_cost_cny"
        ],
        "usage_complete": state["budget"]["usage_complete"],
        "artifact_directory": output.relative_to(root).as_posix(),
        "report": (output / "public_regression_report.json")
        .relative_to(root)
        .as_posix(),
        "summary": (output / "public_regression_summary.md")
        .relative_to(root)
        .as_posix(),
    }
