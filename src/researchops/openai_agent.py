from __future__ import annotations

import hashlib
import importlib.metadata
import json
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from .audit import AuditError, safe_audit_value, sha256_json
from .tool_runtime import ControlledToolExecutor, ToolRuntimeError


DEFAULT_AGENT_MODEL = "gpt-5.6"


class OpenAIAgentIntegrationError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code

    def to_dict(self) -> dict[str, str]:
        return {"status": "error", "error_code": self.code, "message": str(self)}


@dataclass(frozen=True)
class OpenAIRunOutcome:
    status: str
    run_id: str
    final_output: str | None
    interruptions: tuple[dict[str, Any], ...]
    serialized_state: str | None

    def to_public_dict(self) -> dict[str, Any]:
        """The serialized SDK state is intentionally not printed or audited."""

        return {
            "status": self.status,
            "run_id": self.run_id,
            "final_output": self.final_output,
            "interruptions": list(self.interruptions),
            "has_resumable_state": self.serialized_state is not None,
        }


def sdk_status() -> dict[str, Any]:
    try:
        version = importlib.metadata.version("openai-agents")
        installed = True
    except importlib.metadata.PackageNotFoundError:
        version = None
        installed = False
    return {
        "installed": installed,
        "version": version,
        "api_key_configured": bool(os.environ.get("OPENAI_API_KEY")),
    }


def build_openai_agent(
    executor: ControlledToolExecutor,
    run_id: str,
    *,
    bundle_id: str = "phase3",
    model: str = DEFAULT_AGENT_MODEL,
):
    """Construct an Agent without making any network request."""

    try:
        from agents import Agent, ModelSettings, RunContextWrapper, function_tool
    except ImportError as exc:
        raise OpenAIAgentIntegrationError(
            "sdk_not_installed",
            "未安装 OpenAI Agents SDK；请安装 requirements.lock。",
        ) from exc

    async def read_aggregate_evidence(context) -> str:
        """Read the already-computed, aggregate-only statistical evidence bundle."""

        outcome = executor.propose(
            run_id,
            "read_aggregate_evidence",
            {"bundle_id": bundle_id},
        )
        return json.dumps(outcome.result, ensure_ascii=False, sort_keys=True, allow_nan=False)

    read_aggregate_evidence.__annotations__["context"] = RunContextWrapper[dict[str, Any]]
    read_tool = function_tool(
        read_aggregate_evidence,
        failure_error_function=_safe_sdk_tool_failure,
        timeout=30.0,
    )

    async def publish_aggregate_results(context, release_name: str) -> str:
        """Publish aggregate evidence to a controlled project release directory."""

        sdk_call_id = getattr(context, "tool_call_id", None)
        if not isinstance(sdk_call_id, str) or not sdk_call_id:
            raise ToolRuntimeError("sdk_call_id_invalid", "SDK 工具调用缺少 call_id。")
        local_call_id = _sdk_local_call_id(run_id, sdk_call_id)
        outcome = executor.execute(
            local_call_id,
            arguments={"bundle_id": bundle_id, "release_name": release_name},
        )
        return json.dumps(outcome.result, ensure_ascii=False, sort_keys=True, allow_nan=False)

    async def publish_needs_approval(
        context, arguments: dict[str, Any], sdk_call_id: str
    ) -> bool:
        del context
        if set(arguments) != {"release_name"} or not isinstance(
            arguments.get("release_name"), str
        ):
            raise ToolRuntimeError(
                "tool_arguments_invalid", "发布工具参数不符合受控契约。"
            )
        _ensure_sdk_publish_proposal(
            executor,
            run_id,
            sdk_call_id,
            bundle_id=bundle_id,
            release_name=arguments["release_name"],
        )
        return True

    publish_aggregate_results.__annotations__["context"] = RunContextWrapper[dict[str, Any]]
    publish_aggregate_results.__annotations__["release_name"] = str
    publish_aggregate_results.__annotations__["return"] = str
    publish_tool = function_tool(
        publish_aggregate_results,
        needs_approval=publish_needs_approval,
        failure_error_function=_safe_sdk_tool_failure,
        timeout=30.0,
    )

    return Agent(
        name="ResearchOps Scientific Data Analysis Agent",
        model=model,
        model_settings=ModelSettings(
            parallel_tool_calls=False,
            max_tokens=1_200,
            store=False,
            include_usage=True,
            preserve_raw_usage=True,
        ),
        instructions=(
            "You are a controlled scientific-analysis orchestrator. "
            "Use only registered tools. Read aggregate evidence before drawing conclusions. "
            "Never request raw rows, participant identifiers, arbitrary paths, SQL, shell commands, "
            "or secrets. Quantitative claims must cite evidence_id. State the contrast direction "
            "as treatment - control and distinguish available-case analysis from full ITT. "
            "Publishing is optional and must be proposed only when the user explicitly asks for it."
        ),
        tools=[read_tool, publish_tool],
    )


async def run_openai_agent(
    executor: ControlledToolExecutor,
    run_id: str,
    prompt: str,
    *,
    api_key: str | None = None,
    model: str = DEFAULT_AGENT_MODEL,
    tracing_disabled: bool = True,
) -> OpenAIRunOutcome:
    key = _require_api_key(api_key)
    try:
        from agents import RunConfig, Runner, set_default_openai_key
    except ImportError as exc:
        raise OpenAIAgentIntegrationError(
            "sdk_not_installed", "未安装 OpenAI Agents SDK。"
        ) from exc
    set_default_openai_key(key, use_for_tracing=not tracing_disabled)
    agent = build_openai_agent(executor, run_id, model=model)
    started_at = datetime.now(timezone.utc).isoformat()
    started = time.perf_counter()
    try:
        result = await Runner.run(
            agent,
            prompt,
            context={"approval_actor": None},
            max_turns=8,
            run_config=RunConfig(
                tracing_disabled=tracing_disabled,
                trace_include_sensitive_data=False,
                workflow_name="ResearchOps Phase 4",
            ),
        )
    except Exception as exc:
        _record_failed_model_run(executor, run_id, model, started_at, started)
        _fail_run_if_possible(executor, run_id, "agent_runner_failed")
        raise OpenAIAgentIntegrationError(
            "agent_runner_failed",
            f"Agents SDK 运行失败：{type(exc).__name__}。详细异常未写入审计账本。",
        ) from exc
    outcome = _capture_result(executor, run_id, result)
    _record_model_result(executor, run_id, model, started_at, started, result, outcome.status)
    executor.ledger.set_run_status(
        run_id, "waiting_approval" if outcome.status == "waiting_approval" else "completed"
    )
    return outcome


async def resume_openai_agent(
    executor: ControlledToolExecutor,
    run_id: str,
    serialized_state: str,
    *,
    sdk_call_id: str,
    decision: str,
    approver: str,
    api_key: str | None = None,
    model: str = DEFAULT_AGENT_MODEL,
    tracing_disabled: bool = True,
) -> OpenAIRunOutcome:
    key = _require_api_key(api_key)
    if decision not in {"approve", "reject"}:
        raise OpenAIAgentIntegrationError(
            "sdk_approval_decision_invalid", "decision 必须是 approve 或 reject。"
        )
    if not approver.strip():
        raise OpenAIAgentIntegrationError(
            "sdk_approver_invalid", "审批者标识不能为空。"
        )
    try:
        from agents import RunConfig, RunState, Runner, set_default_openai_key
    except ImportError as exc:
        raise OpenAIAgentIntegrationError(
            "sdk_not_installed", "未安装 OpenAI Agents SDK。"
        ) from exc
    set_default_openai_key(key, use_for_tracing=not tracing_disabled)
    agent = build_openai_agent(executor, run_id, model=model)
    state = await RunState.from_string(
        agent,
        serialized_state,
        context_override={"approval_actor": approver if decision == "approve" else None},
    )
    prior_response_count = len(getattr(state, "_model_responses", ()) or ())
    interruptions = state.get_interruptions()
    selected = next((item for item in interruptions if item.call_id == sdk_call_id), None)
    if selected is None:
        raise OpenAIAgentIntegrationError(
            "sdk_interruption_not_found", "找不到指定的 SDK 工具审批中断。"
        )
    selected_arguments = _sdk_arguments(selected.arguments)
    if selected.name == "publish_aggregate_results":
        if set(selected_arguments) != {"release_name"} or not isinstance(
            selected_arguments.get("release_name"), str
        ):
            raise OpenAIAgentIntegrationError(
                "sdk_arguments_invalid", "发布审批参数不符合受控契约。"
            )
        local_call_id = _ensure_sdk_publish_proposal(
            executor,
            run_id,
            sdk_call_id,
            bundle_id="phase3",
            release_name=selected_arguments["release_name"],
        )
        executor.decide(
            local_call_id,
            decision=decision,
            approver=approver,
            reason="Decision bound to the OpenAI Agents SDK interruption.",
        )
    executor.ledger.append_event(
        run_id,
        "sdk_tool_approval_decided",
        {
            "sdk_call_id": sdk_call_id,
            "tool_name": selected.name,
            "arguments_sha256": sha256_json(selected_arguments),
            "decision": decision,
            "approver_subject_hash": hashlib.sha256(approver.encode("utf-8")).hexdigest(),
        },
        actor_kind="human",
    )
    if decision == "approve":
        state.approve(selected)
    else:
        state.reject(selected, rejection_message="Human reviewer rejected this tool call.")
    executor.ledger.set_run_status(run_id, "running")
    started_at = datetime.now(timezone.utc).isoformat()
    started = time.perf_counter()
    try:
        result = await Runner.run(
            agent,
            state,
            max_turns=8,
            run_config=RunConfig(
                tracing_disabled=tracing_disabled,
                trace_include_sensitive_data=False,
                workflow_name="ResearchOps Phase 4",
            ),
        )
    except Exception as exc:
        _record_failed_model_run(executor, run_id, model, started_at, started)
        _fail_run_if_possible(executor, run_id, "agent_runner_failed")
        raise OpenAIAgentIntegrationError(
            "agent_runner_failed",
            f"Agents SDK 恢复失败：{type(exc).__name__}。详细异常未写入审计账本。",
        ) from exc
    outcome = _capture_result(executor, run_id, result)
    _record_model_result(
        executor,
        run_id,
        model,
        started_at,
        started,
        result,
        outcome.status,
        response_start=prior_response_count,
    )
    executor.ledger.set_run_status(
        run_id, "waiting_approval" if outcome.status == "waiting_approval" else "completed"
    )
    return outcome


def _capture_result(
    executor: ControlledToolExecutor, run_id: str, result: Any
) -> OpenAIRunOutcome:
    state = result.to_state()
    interruptions = state.get_interruptions()
    public_interruptions = []
    for item in interruptions:
        arguments = _sdk_arguments(item.arguments)
        public_item = {
            "sdk_call_id": item.call_id,
            "tool_name": item.name,
            "arguments_sha256": sha256_json(arguments),
        }
        if item.name == "publish_aggregate_results" and isinstance(
            item.call_id, str
        ):
            public_item["local_call_id"] = _sdk_local_call_id(
                run_id, item.call_id
            )
        public_interruptions.append(public_item)
        executor.ledger.append_event(
            run_id,
            "sdk_tool_interrupted",
            public_item,
            actor_kind="agent_sdk",
        )
    if interruptions:
        return OpenAIRunOutcome(
            status="waiting_approval",
            run_id=run_id,
            final_output=None,
            interruptions=tuple(public_interruptions),
            serialized_state=state.to_string(include_tracing_api_key=False),
        )
    final_output = str(result.final_output) if result.final_output is not None else None
    return OpenAIRunOutcome(
        status="completed",
        run_id=run_id,
        final_output=safe_audit_value(final_output),
        interruptions=(),
        serialized_state=None,
    )


def _sdk_arguments(value: Any) -> dict[str, Any]:
    if isinstance(value, str):
        parsed = json.loads(value)
    else:
        parsed = value
    if not isinstance(parsed, dict):
        raise OpenAIAgentIntegrationError(
            "sdk_arguments_invalid", "SDK 工具审批参数不是 JSON 对象。"
        )
    return parsed


def _sdk_local_call_id(run_id: str, sdk_call_id: str) -> str:
    digest = hashlib.sha256(f"{run_id}\0{sdk_call_id}".encode("utf-8")).hexdigest()
    return f"SDKAPP-{digest[:24].upper()}"


def _ensure_sdk_publish_proposal(
    executor: ControlledToolExecutor,
    run_id: str,
    sdk_call_id: str,
    *,
    bundle_id: str,
    release_name: str,
) -> str:
    if not isinstance(sdk_call_id, str) or not sdk_call_id:
        raise ToolRuntimeError("sdk_call_id_invalid", "SDK 工具调用缺少 call_id。")
    local_call_id = _sdk_local_call_id(run_id, sdk_call_id)
    arguments = {"bundle_id": bundle_id, "release_name": release_name}
    try:
        existing = executor.ledger.get_tool_call(local_call_id)
    except AuditError as exc:
        if exc.code != "tool_call_not_found":
            raise ToolRuntimeError(exc.code, str(exc)) from exc
    else:
        if (
            existing.get("run_id") != run_id
            or existing.get("tool_name") != "publish_aggregate_results"
            or existing.get("safe_args") != arguments
            or existing.get("status") not in {"awaiting_approval", "approved"}
        ):
            raise ToolRuntimeError(
                "tool_approval_mismatch",
                "SDK call_id 已绑定到不同或不可恢复的本地审批范围。",
            )
        return local_call_id
    outcome = executor.propose(
        run_id,
        "publish_aggregate_results",
        arguments,
        call_id=local_call_id,
    )
    if outcome.status != "awaiting_approval" or outcome.requires_approval is not True:
        raise ToolRuntimeError(
            "tool_approval_required", "发布提案未停在本地待审批状态。"
        )
    return local_call_id


def _safe_sdk_tool_failure(context: Any, error: Exception) -> str:
    del context
    candidate = getattr(error, "code", None)
    code = (
        candidate
        if isinstance(candidate, str)
        and candidate.replace("_", "").isalnum()
        and candidate[:1].islower()
        else "tool_execution_failed"
    )
    return json.dumps(
        {"status": "error", "error_code": code},
        sort_keys=True,
        separators=(",", ":"),
    )


def _require_api_key(explicit_key: str | None) -> str:
    key = explicit_key or os.environ.get("OPENAI_API_KEY")
    if not key:
        raise OpenAIAgentIntegrationError(
            "api_key_missing",
            "未配置 OPENAI_API_KEY；为避免隐式网络调用，真实 Agent 尚未启动。",
        )
    return key


def _record_model_result(
    executor: ControlledToolExecutor,
    run_id: str,
    model: str,
    started_at: str,
    started: float,
    result: Any,
    status: str,
    *,
    response_start: int = 0,
) -> None:
    elapsed_ms = max((time.perf_counter() - started) * 1000.0, 0.0)
    responses = list(getattr(result, "raw_responses", ()) or ())
    new_responses = responses[response_start:]
    if new_responses:
        per_response_latency = elapsed_ms / len(new_responses)
        for index, response in enumerate(new_responses):
            usage = getattr(response, "usage", None)
            details = getattr(usage, "input_tokens_details", None)
            cached_tokens = getattr(details, "cached_tokens", None)
            executor.ledger.record_model_call(
                run_id,
                provider="openai",
                model=model,
                started_at_utc=started_at,
                latency_ms=per_response_latency,
                input_tokens=_optional_nonnegative_int(
                    getattr(usage, "input_tokens", None)
                ),
                output_tokens=_optional_nonnegative_int(
                    getattr(usage, "output_tokens", None)
                ),
                cached_tokens=_optional_nonnegative_int(cached_tokens),
                cost_usd=None,
                outcome=(
                    "interrupted"
                    if status == "waiting_approval" and index == len(new_responses) - 1
                    else "succeeded"
                ),
            )
        return

    # Test doubles and older compatible result objects may expose aggregate usage only.
    # On resume, an empty delta must not re-record the cumulative aggregate.
    if response_start:
        return
    usage = getattr(getattr(result, "context_wrapper", None), "usage", None)
    details = getattr(usage, "input_tokens_details", None)
    cached_tokens = getattr(details, "cached_tokens", None)
    executor.ledger.record_model_call(
        run_id,
        provider="openai",
        model=model,
        started_at_utc=started_at,
        latency_ms=elapsed_ms,
        input_tokens=_optional_nonnegative_int(getattr(usage, "input_tokens", None)),
        output_tokens=_optional_nonnegative_int(getattr(usage, "output_tokens", None)),
        cached_tokens=_optional_nonnegative_int(cached_tokens),
        cost_usd=None,
        outcome="interrupted" if status == "waiting_approval" else "succeeded",
    )


def _optional_nonnegative_int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _record_failed_model_run(
    executor: ControlledToolExecutor,
    run_id: str,
    model: str,
    started_at: str,
    started: float,
) -> None:
    executor.ledger.record_model_call(
        run_id,
        provider="openai",
        model=model,
        started_at_utc=started_at,
        latency_ms=max((time.perf_counter() - started) * 1000.0, 0.0),
        input_tokens=None,
        output_tokens=None,
        cached_tokens=None,
        cost_usd=None,
        outcome="failed",
        error_code="agent_runner_failed",
    )


def _fail_run_if_possible(
    executor: ControlledToolExecutor, run_id: str, error_code: str
) -> None:
    try:
        executor.ledger.set_run_status(run_id, "failed", terminal_error_code=error_code)
    except Exception:
        pass
