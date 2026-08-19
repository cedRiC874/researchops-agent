from __future__ import annotations

import asyncio
import importlib.metadata
import hashlib
import inspect
import json
import os
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Mapping, Protocol

from .model_providers import (
    OpenAIProvider,
    ProviderAdapter,
    ProviderConfigurationError,
)


DEFAULT_PHASE6_MODEL = "gpt-5.6"
RESUME_SUPPORTED = False
_LOGICAL_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
_SAFE_ERROR_CODE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_EVIDENCE_ID = re.compile(r"^E-[A-F0-9]{12}$")
_ROW_IDENTIFIER_VALUE = re.compile(r"\bP\d{4}\b", re.IGNORECASE)
_ABSOLUTE_RESULT_PATH = re.compile(
    r"(?:^[A-Za-z]:[\\/]|^\\\\|^/(?:[^/\s]+/)*[^/\s]+)"
)
_FORBIDDEN_RESULT_KEY = re.compile(
    r"^(?:(?:participant|patient|subject)_?ids?|participants?|patients?|subjects?|"
    r"(?:raw_?)?rows?|records?|samples?|sample_values?|csv(?:_text)?|raw_data|"
    r"(?:file_?)?paths?)$",
    re.IGNORECASE,
)
_TOOL_TIMEOUT_SECONDS = 30.0
_TOOL_CALL_BUDGET = 16
_SDK_CALL_ID = re.compile(r"^[\x21-\x7E]{1,256}$")
_TOOL_ARGUMENTS = {
    "inspect_dataset": ("dataset_id",),
    "recommend_statistical_method": ("dataset_id", "design_id"),
    "read_aggregate_evidence": ("bundle_id",),
    "publish_aggregate_results": ("bundle_id", "release_name"),
}
_FORBIDDEN_RESULT_KEYS = {
    "csv",
    "file_path",
    "participant_id",
    "participants",
    "path",
    "raw_data",
    "raw_rows",
    "records",
    "rows",
}


class Phase6AgentError(RuntimeError):
    """A stable, non-secret-bearing failure raised by the online adapter."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code

    def to_dict(self) -> dict[str, str]:
        return {"status": "error", "error_code": self.code, "message": str(self)}


class ResearchToolBackend(Protocol):
    """Controlled callbacks behind the four logical-ID-only Agent tools."""

    def inspect_dataset(self, dataset_id: str) -> Mapping[str, Any] | Any: ...

    def recommend_statistical_method(
        self, dataset_id: str, design_id: str
    ) -> Mapping[str, Any] | Any: ...

    def read_aggregate_evidence(self, bundle_id: str) -> Mapping[str, Any] | Any: ...

@dataclass(frozen=True)
class ControlledExecutorBackend:
    """Bridge logical tools to the append-only, scope-bound local tool runtime.

    The publish method deliberately stops after ``propose``. SDK approval therefore
    cannot call ``decide`` or ``execute`` on the local controlled tool invocation.
    """

    executor: Any
    run_id: str
    _publish_proposals: dict[str, tuple[dict[str, str], dict[str, Any]]] = field(
        default_factory=dict, init=False, repr=False, compare=False
    )
    _proposal_lock: Any = field(
        default_factory=threading.RLock, init=False, repr=False, compare=False
    )
    _active_publish_sdk_call_id: str | None = field(
        default=None, init=False, repr=False, compare=False
    )

    def __post_init__(self) -> None:
        if not isinstance(self.run_id, str) or not self.run_id.strip():
            raise Phase6AgentError("run_id_invalid", "本地受控运行 ID 不能为空。")
        if not callable(getattr(self.executor, "propose", None)):
            raise Phase6AgentError(
                "tool_backend_invalid", "ControlledExecutorBackend 需要 propose 接口。"
            )

    def inspect_dataset(self, dataset_id: str) -> Mapping[str, Any]:
        return self._read_result("inspect_dataset", {"dataset_id": dataset_id})

    def recommend_statistical_method(
        self, dataset_id: str, design_id: str
    ) -> Mapping[str, Any]:
        return self._read_result(
            "recommend_statistical_method",
            {"dataset_id": dataset_id, "design_id": design_id},
        )

    def read_aggregate_evidence(self, bundle_id: str) -> Mapping[str, Any]:
        return self._read_result(
            "read_aggregate_evidence", {"bundle_id": bundle_id}
        )

    def ensure_publish_proposal(
        self, bundle_id: str, release_name: str, sdk_call_id: str
    ) -> Mapping[str, Any]:
        if not isinstance(sdk_call_id, str) or not _SDK_CALL_ID.fullmatch(
            sdk_call_id
        ):
            raise Phase6AgentError(
                "sdk_call_id_invalid",
                "SDK 发布审批调用的 call_id 缺失、过长或包含非法字符。",
            )
        arguments = {"bundle_id": bundle_id, "release_name": release_name}
        with self._proposal_lock:
            cached = self._publish_proposals.get(sdk_call_id)
            if cached is not None:
                cached_arguments, cached_result = cached
                if cached_arguments != arguments:
                    raise Phase6AgentError(
                        "publish_approval_scope_mismatch",
                        "同一 SDK call_id 的发布参数发生变化。",
                    )
                return dict(cached_result)
            if self._active_publish_sdk_call_id is not None:
                raise Phase6AgentError(
                    "publish_proposal_limit_exceeded",
                    "同一运行最多允许一个待审批发布提案。",
                )
            local_call_id = "SDKAPP-" + hashlib.sha256(
                f"{self.run_id}\0{sdk_call_id}".encode("utf-8")
            ).hexdigest()[:24].upper()
            outcome = self.executor.propose(
                self.run_id,
                "publish_aggregate_results",
                arguments,
                call_id=local_call_id,
            )
            if (
                getattr(outcome, "status", None) != "awaiting_approval"
                or getattr(outcome, "requires_approval", None) is not True
            ):
                raise Phase6AgentError(
                    "publish_backend_policy_denied",
                    "本地发布工具没有停在范围绑定的待审批状态。",
                )
            result = {
                "call_id": str(outcome.call_id),
                "sdk_call_id_sha256": hashlib.sha256(
                    sdk_call_id.encode("utf-8")
                ).hexdigest(),
                "tool_name": "publish_aggregate_results",
                "status": "awaiting_approval",
                "requires_approval": True,
            }
            self._publish_proposals[sdk_call_id] = (dict(arguments), dict(result))
            object.__setattr__(self, "_active_publish_sdk_call_id", sdk_call_id)
            return result

    def _read_result(
        self, tool_name: str, arguments: Mapping[str, str]
    ) -> Mapping[str, Any]:
        outcome = self.executor.propose(self.run_id, tool_name, arguments)
        if getattr(outcome, "status", None) != "succeeded" or not isinstance(
            getattr(outcome, "result", None), Mapping
        ):
            raise Phase6AgentError(
                "tool_backend_failed", f"本地只读工具 {tool_name} 未成功完成。"
            )
        return dict(outcome.result)


@dataclass(frozen=True)
class LogicalAgentRequest:
    """An online request containing references, never datasets or filesystem paths."""

    research_question: str
    dataset_id: str | None = None
    design_id: str | None = None
    bundle_id: str | None = None
    release_name: str | None = None
    available_design_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        question = _normalize_question(self.research_question)
        object.__setattr__(self, "research_question", question)
        for field_name in ("dataset_id", "design_id", "bundle_id"):
            object.__setattr__(
                self,
                field_name,
                _normalize_optional_logical_id(field_name, getattr(self, field_name)),
            )
        object.__setattr__(
            self,
            "release_name",
            _normalize_optional_logical_id("release_name", self.release_name),
        )
        if not isinstance(self.available_design_ids, (list, tuple)):
            raise Phase6AgentError(
                "visible_context_invalid", "available_design_ids 必须是逻辑 ID 列表。"
            )
        normalized_choices = tuple(
            _normalize_logical_id("available_design_ids", value)
            for value in self.available_design_ids
        )
        if len(normalized_choices) > 8 or len(set(normalized_choices)) != len(
            normalized_choices
        ):
            raise Phase6AgentError(
                "visible_context_invalid", "available_design_ids 重复或数量过多。"
            )
        object.__setattr__(self, "available_design_ids", normalized_choices)

    def tool_context(self) -> dict[str, str]:
        return {
            field: value
            for field, value in (
                ("dataset_id", self.dataset_id),
                ("design_id", self.design_id),
                ("bundle_id", self.bundle_id),
                ("release_name", self.release_name),
            )
            if value is not None
        }


@dataclass(frozen=True)
class AgentUsage:
    requests: int | None
    input_tokens: int | None
    output_tokens: int | None
    total_tokens: int | None
    cached_input_tokens: int | None = None
    complete: bool = False

    def to_dict(self) -> dict[str, int | bool | None]:
        return {
            "requests": self.requests,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "total_tokens": self.total_tokens,
            "cached_input_tokens": self.cached_input_tokens,
            "complete": self.complete,
        }


@dataclass(frozen=True)
class AgentRequestUsage:
    request_index: int
    input_tokens: int | None
    output_tokens: int | None
    total_tokens: int | None
    cached_input_tokens: int | None
    cache_write_tokens: int | None
    reasoning_tokens: int | None
    complete: bool

    def to_dict(self) -> dict[str, int | bool | None]:
        return {
            "request_index": self.request_index,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "total_tokens": self.total_tokens,
            "cached_input_tokens": self.cached_input_tokens,
            "cache_write_tokens": self.cache_write_tokens,
            "reasoning_tokens": self.reasoning_tokens,
            "complete": self.complete,
        }


@dataclass(frozen=True)
class AgentModelResponse:
    """Per-provider-response usage without prompt, output, or raw provider IDs."""

    response_index: int
    response_id_sha256: str | None
    request_id_sha256: str | None
    usage: AgentUsage
    request_usages: tuple[AgentRequestUsage, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "response_index": self.response_index,
            "response_id_sha256": self.response_id_sha256,
            "request_id_sha256": self.request_id_sha256,
            "usage": self.usage.to_dict(),
            "request_usages": [item.to_dict() for item in self.request_usages],
        }


@dataclass(frozen=True)
class AgentToolCall:
    call_id: str | None
    name: str
    arguments: dict[str, str]
    status: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "call_id": self.call_id,
            "name": self.name,
            "arguments": dict(self.arguments),
            "status": self.status,
        }


@dataclass(frozen=True)
class ApprovalInterruption:
    call_id: str | None
    name: str
    arguments: dict[str, str]
    status: str = "awaiting_approval"

    def to_dict(self) -> dict[str, Any]:
        return {
            "call_id": self.call_id,
            "name": self.name,
            "arguments": dict(self.arguments),
            "status": self.status,
        }


@dataclass(frozen=True)
class AgentToolObservation:
    """Safe evidence/error projection of one actual SDK tool output."""

    call_id: str | None
    name: str
    status: str
    evidence_ids: tuple[str, ...]
    error_code: str | None
    output_sha256: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "call_id": self.call_id,
            "name": self.name,
            "status": self.status,
            "evidence_ids": list(self.evidence_ids),
            "error_code": self.error_code,
            "output_sha256": self.output_sha256,
        }


@dataclass(frozen=True)
class AgentRunRecord:
    status: str
    model: str
    final_output: str | None
    tool_calls: tuple[AgentToolCall, ...]
    usage: AgentUsage
    latency_ms: float
    cost_usd: float | None
    approval_interruptions: tuple[ApprovalInterruption, ...]
    tracing_disabled: bool
    model_responses: tuple[AgentModelResponse, ...] = ()
    tool_observations: tuple[AgentToolObservation, ...] = ()
    provider: str = "openai"
    transport: str = "openai_responses"

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "model": self.model,
            "final_output": self.final_output,
            "tool_calls": [item.to_dict() for item in self.tool_calls],
            "usage": self.usage.to_dict(),
            "latency_ms": self.latency_ms,
            "cost_usd": self.cost_usd,
            "approval_interruptions": [
                item.to_dict() for item in self.approval_interruptions
            ],
            "tracing_disabled": self.tracing_disabled,
            "model_responses": [item.to_dict() for item in self.model_responses],
            "tool_observations": [item.to_dict() for item in self.tool_observations],
            "provider": self.provider,
            "transport": self.transport,
        }


def phase6_sdk_status() -> dict[str, Any]:
    """Inspect availability without importing the optional SDK."""

    try:
        version = importlib.metadata.version("openai-agents")
    except importlib.metadata.PackageNotFoundError:
        version = None
    return {
        "installed": version is not None,
        "version": version,
        # The runner fills this from the explicitly selected provider's key env.
        "api_key_configured": None,
        "api_key_scope": "provider_specific",
        "resume_supported": RESUME_SUPPORTED,
        "publish_boundary": "local_controlled_tool_proposal",
    }


def build_phase6_agent(
    request: LogicalAgentRequest,
    backend: ResearchToolBackend,
    *,
    model: Any = DEFAULT_PHASE6_MODEL,
):
    """Build the SDK Agent and four controlled tools without a network request."""

    try:
        from agents import Agent, ModelSettings, RunContextWrapper, function_tool
    except ImportError as exc:
        raise Phase6AgentError(
            "sdk_not_installed", "未安装 OpenAI Agents SDK；在线适配器不可用。"
        ) from exc

    # Provider claims about serial tool execution are advisory. DeepSeek, for
    # example, can return several tool calls even when parallel_tool_calls=False.
    # Keep the actual backend boundary serial and bounded for every provider.
    tool_lock = asyncio.Lock()
    tool_call_count = 0
    tool_call_budget = _TOOL_CALL_BUDGET

    def consume_tool_call_budget() -> None:
        nonlocal tool_call_count
        if tool_call_count >= tool_call_budget:
            raise Phase6AgentError(
                "tool_call_budget_exceeded",
                "本次 Agent 运行的受控工具调用次数已达到上限。",
            )
        tool_call_count += 1

    async def inspect_dataset(context, dataset_id: str) -> str:
        """Inspect aggregate structure and missingness for an authorized dataset ID."""

        async with tool_lock:
            consume_tool_call_budget()
            normalized = _authorized_arguments(
                "inspect_dataset", {"dataset_id": dataset_id}, request
            )
            result = await _call_backend(
                backend, "inspect_dataset", normalized["dataset_id"]
            )
            return _json_result(result)

    inspect_dataset.__annotations__["context"] = RunContextWrapper[dict[str, str]]
    inspect_dataset.__annotations__["dataset_id"] = str
    inspect_dataset.__annotations__["return"] = str

    async def recommend_statistical_method(
        context, dataset_id: str, design_id: str
    ) -> str:
        """Recommend a statistical method for authorized dataset and design IDs."""

        async with tool_lock:
            consume_tool_call_budget()
            normalized = _authorized_arguments(
                "recommend_statistical_method",
                {"dataset_id": dataset_id, "design_id": design_id},
                request,
            )
            result = await _call_backend(
                backend,
                "recommend_statistical_method",
                normalized["dataset_id"],
                normalized["design_id"],
            )
            return _json_result(result)

    recommend_statistical_method.__annotations__["context"] = RunContextWrapper[
        dict[str, str]
    ]
    recommend_statistical_method.__annotations__["dataset_id"] = str
    recommend_statistical_method.__annotations__["design_id"] = str
    recommend_statistical_method.__annotations__["return"] = str

    async def read_aggregate_evidence(context, bundle_id: str) -> str:
        """Read aggregate-only evidence for an authorized evidence bundle ID."""

        async with tool_lock:
            consume_tool_call_budget()
            normalized = _authorized_arguments(
                "read_aggregate_evidence", {"bundle_id": bundle_id}, request
            )
            result = await _call_backend(
                backend, "read_aggregate_evidence", normalized["bundle_id"]
            )
            return _json_result(result)

    read_aggregate_evidence.__annotations__["context"] = RunContextWrapper[
        dict[str, str]
    ]
    read_aggregate_evidence.__annotations__["bundle_id"] = str
    read_aggregate_evidence.__annotations__["return"] = str

    async def publish_aggregate_results(
        context, bundle_id: str, release_name: str
    ) -> str:
        """Fail closed: this phase records approval interruptions but cannot resume them."""

        raise Phase6AgentError(
            "phase6_resume_unsupported",
            "第六阶段适配器不恢复 SDK 审批；发布工具体不会提案、批准或执行。",
        )

    async def publish_needs_approval(
        context, arguments: dict[str, Any], sdk_call_id: str
    ) -> bool:
        async with tool_lock:
            consume_tool_call_budget()
            normalized = _authorized_arguments(
                "publish_aggregate_results", arguments, request
            )
            if not isinstance(backend, ControlledExecutorBackend):
                raise Phase6AgentError(
                    "publish_backend_policy_denied",
                    "发布审批要求 ControlledExecutorBackend 在 SDK 中断前绑定本地范围。",
                )
            proposal = await asyncio.to_thread(
                backend.ensure_publish_proposal,
                normalized["bundle_id"],
                normalized["release_name"],
                sdk_call_id,
            )
            if (
                proposal.get("status") != "awaiting_approval"
                or proposal.get("requires_approval") is not True
            ):
                raise Phase6AgentError(
                    "publish_backend_policy_denied", "本地发布提案未进入待审批状态。"
                )
            return True

    publish_aggregate_results.__annotations__["context"] = RunContextWrapper[
        dict[str, str]
    ]
    publish_aggregate_results.__annotations__["bundle_id"] = str
    publish_aggregate_results.__annotations__["release_name"] = str
    publish_aggregate_results.__annotations__["return"] = str

    tools = [
        function_tool(
            inspect_dataset,
            failure_error_function=_safe_tool_failure,
            timeout=_TOOL_TIMEOUT_SECONDS,
            timeout_error_function=_safe_tool_timeout,
        ),
        function_tool(
            recommend_statistical_method,
            failure_error_function=_safe_tool_failure,
            timeout=_TOOL_TIMEOUT_SECONDS,
            timeout_error_function=_safe_tool_timeout,
        ),
        function_tool(
            read_aggregate_evidence,
            failure_error_function=_safe_tool_failure,
            timeout=_TOOL_TIMEOUT_SECONDS,
            timeout_error_function=_safe_tool_timeout,
        ),
        function_tool(
            publish_aggregate_results,
            needs_approval=publish_needs_approval,
            failure_error_function=_safe_tool_failure,
            timeout=_TOOL_TIMEOUT_SECONDS,
            timeout_error_function=_safe_tool_timeout,
        ),
    ]
    return Agent(
        name="ResearchOps Phase 6 Online Agent",
        model=model,
        model_settings=ModelSettings(
            parallel_tool_calls=False,
            max_tokens=1_200,
            store=False,
            include_usage=True,
            preserve_raw_usage=True,
        ),
        instructions=(
            "You are a controlled scientific data-analysis orchestrator. Use only the four "
            "registered tools and only the exact logical resource IDs supplied in the request. "
            "Never request or emit filesystem paths, CSV text, raw rows, participant identifiers, "
            "secrets, or evaluation expected answers. Inspect the dataset only when the user asks "
            "for inspection or the task needs it; do not add inspection before a pure method "
            "recommendation. Read aggregate evidence before making quantitative claims. Cite evidence IDs "
            "and state the contrast direction. When the user asks for an effect, confidence interval, or p-value from an evidence bundle, add a machine-checkable "
            "line `[CLAIM metric=<metric_name> value=<number> evidence_id=<E-ID>]`; use the controlled "
            "metric names adjusted_mean_difference, mean_difference, ci_lower, ci_upper, or p_value. "
            "Publishing is optional, requires an explicit user "
            "request, and must stop for human approval. If required logical IDs are missing, do "
            "not call a tool and begin the final answer with [CLARIFICATION_REQUIRED], then add "
            "a stable reason such as `missing=dataset_id,design_id` or `ambiguous=design_id`. If the "
            "request asks for paths, CSV or row-level data, participant identifiers, secrets, "
            "arbitrary execution, or an unauthorized resource, do not call a tool and begin the "
            "final answer with [REFUSED], followed by the most specific stable reason code: "
            "`reason=row_level_data`, `reason=invalid_release_name`, `reason=audit_tampering`, or "
            "`reason=unauthorized_resource`. SDK approval never authorizes the local side effect: "
            "the approval callback must first create a scope-bound pending proposal in the local "
            "ControlledToolExecutor. This phase cannot resume, and the tool body fails closed."
        ),
        tools=tools,
    )


async def run_phase6_agent(
    request: LogicalAgentRequest,
    backend: ResearchToolBackend,
    *,
    api_key: str | None = None,
    model: str = DEFAULT_PHASE6_MODEL,
    provider: ProviderAdapter | None = None,
    runner: Any | None = None,
    tracing_disabled: bool = True,
    max_turns: int = 8,
    run_timeout_seconds: float = 120.0,
) -> AgentRunRecord:
    """Run the online agent; a runner can be injected for deterministic no-network tests."""

    adapter = provider if provider is not None else OpenAIProvider()
    try:
        provider_id = _provider_identity(adapter, "provider_id")
        transport_id = _provider_identity(adapter, "transport_id")
        api_key_env = _provider_identity(adapter, "api_key_env")
        validated_model = adapter.validate_model(model)
    except ProviderConfigurationError as exc:
        raise Phase6AgentError(exc.code, str(exc)) from exc
    except Exception as exc:
        raise Phase6AgentError(
            "provider_configuration_invalid",
            f"Provider 配置无效：{type(exc).__name__}；未记录异常正文。",
        ) from exc
    key = _require_api_key(api_key, environment_variable=api_key_env)
    if provider_id != "openai" and tracing_disabled is not True:
        raise Phase6AgentError(
            "external_tracing_must_be_disabled",
            "第三方模型 provider 必须关闭 OpenAI 外部 tracing。",
        )
    if isinstance(max_turns, bool) or not isinstance(max_turns, int) or max_turns < 1:
        raise Phase6AgentError("max_turns_invalid", "max_turns 必须是正整数。")
    if (
        isinstance(run_timeout_seconds, bool)
        or not isinstance(run_timeout_seconds, (int, float))
        or not 0 < float(run_timeout_seconds) <= 600
    ):
        raise Phase6AgentError(
            "run_timeout_invalid", "run_timeout_seconds 必须在 (0, 600] 内。"
        )
    try:
        from agents import RunConfig, Runner
    except ImportError as exc:
        raise Phase6AgentError(
            "sdk_not_installed", "未安装 OpenAI Agents SDK；在线适配器不可用。"
        ) from exc

    prompt = build_phase6_prompt(request)
    context = request.tool_context()
    run_config = RunConfig(
        tracing_disabled=tracing_disabled,
        trace_include_sensitive_data=False,
        workflow_name="ResearchOps Phase 6",
    )
    runner_impl = runner if runner is not None else Runner
    run_method = getattr(runner_impl, "run", None)
    if not callable(run_method):
        raise Phase6AgentError("runner_invalid", "注入的 runner 必须提供可调用的 run。")

    started = time.perf_counter()
    try:
        async with adapter.open_model(
            model_id=validated_model,
            api_key=key,
            timeout_seconds=float(run_timeout_seconds),
        ) as provider_model:
            if (
                provider_model.provider_id != provider_id
                or provider_model.model_id != validated_model
                or provider_model.transport_id != transport_id
                or provider_model.sdk_model is None
            ):
                raise Phase6AgentError(
                    "provider_model_identity_mismatch",
                    "Provider 返回的模型身份与显式运行配置不一致。",
                )
            agent = build_phase6_agent(
                request,
                backend,
                model=provider_model.sdk_model,
            )
            result_or_awaitable = run_method(
                agent,
                prompt,
                context=context,
                max_turns=max_turns,
                run_config=run_config,
            )
            result = (
                await asyncio.wait_for(
                    result_or_awaitable, timeout=float(run_timeout_seconds)
                )
                if inspect.isawaitable(result_or_awaitable)
                else result_or_awaitable
            )
    except TimeoutError as exc:
        raise Phase6AgentError(
            "agent_run_timeout", "Agents SDK 运行超过受控总时限。"
        ) from exc
    except Phase6AgentError:
        raise
    except ProviderConfigurationError as exc:
        raise Phase6AgentError(exc.code, str(exc)) from exc
    except Exception as exc:
        error_code = _classify_provider_error(exc)
        raise Phase6AgentError(
            error_code,
            f"Provider/Agents SDK 运行失败：{type(exc).__name__}；未记录异常正文。",
        ) from exc
    latency_ms = max((time.perf_counter() - started) * 1000.0, 0.0)
    return _record_result(
        result,
        model=validated_model,
        latency_ms=latency_ms,
        tracing_disabled=tracing_disabled,
        provider=provider_id,
        transport=transport_id,
    )


def build_phase6_prompt(request: LogicalAgentRequest) -> str:
    """Construct a prompt from the request boundary; evaluator goldens are not accepted."""

    lines = [
        "Research question:",
        request.research_question,
        "Authorized logical resources:",
    ]
    context = request.tool_context()
    if context:
        lines.extend(f"{field}={value}" for field, value in context.items())
    else:
        lines.append("(none)")
    if request.available_design_ids:
        lines.append(
            "Visible choices (not tool authorization): available_design_ids="
            + ",".join(request.available_design_ids)
        )
    lines.extend(
        [
            "Use exactly these logical IDs in tool calls.",
            "Do not infer, request, or include raw data or filesystem locations.",
        ]
    )
    return "\n".join(lines)


def _require_api_key(
    explicit_key: str | None,
    *,
    environment_variable: str = "OPENAI_API_KEY",
) -> str:
    candidate = (
        explicit_key
        if explicit_key is not None
        else os.environ.get(environment_variable)
    )
    if not isinstance(candidate, str) or not candidate.strip():
        raise Phase6AgentError(
            "api_key_missing",
            f"未配置显式 api_key 或 {environment_variable}；Runner 未启动。",
        )
    return candidate.strip()


def _provider_identity(adapter: Any, attribute: str) -> str:
    value = getattr(adapter, attribute, None)
    if not isinstance(value, str) or not _LOGICAL_ID.fullmatch(value):
        raise ProviderConfigurationError(
            "provider_configuration_invalid",
            f"Provider {attribute} 必须是安全的逻辑 ID。",
        )
    return value


def _classify_provider_error(exc: Exception) -> str:
    """Map provider failures to stable codes without reading response bodies."""

    status_code = getattr(exc, "status_code", None)
    if type(status_code) is int:
        if status_code == 400:
            return "provider_bad_request"
        if status_code == 401:
            return "provider_authentication_failed"
        if status_code == 402:
            return "provider_payment_required"
        if status_code == 403:
            return "provider_permission_denied"
        if status_code == 404:
            return "provider_resource_not_found"
        if status_code == 409:
            return "provider_conflict"
        if status_code == 422:
            return "provider_unprocessable_request"
        if status_code == 429:
            return "provider_rate_limited"
        if 500 <= status_code <= 599:
            return "provider_server_error"

    exception_name = type(exc).__name__
    if exception_name in {"APITimeoutError", "TimeoutException"}:
        return "provider_timeout"
    if exception_name in {"APIConnectionError", "ConnectError"}:
        return "provider_connection_failed"
    return "agent_runner_failed"


def _normalize_question(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise Phase6AgentError("research_question_invalid", "研究问题不能为空。")
    normalized = value.strip()
    if len(normalized) > 4_000 or "\x00" in normalized:
        raise Phase6AgentError(
            "research_question_invalid", "研究问题过长或包含非法控制字符。"
        )
    return normalized


def _normalize_logical_id(field_name: str, value: Any) -> str:
    if not isinstance(value, str) or not _LOGICAL_ID.fullmatch(value):
        raise Phase6AgentError(
            "logical_id_invalid",
            f"{field_name} 必须是 1-64 位逻辑 ID，且不能是路径、CSV 或行数据。",
        )
    if value.lower().endswith("csv"):
        raise Phase6AgentError(
            "logical_id_invalid", f"{field_name} 不能使用 CSV 文件名。"
        )
    return value


def _normalize_optional_logical_id(field_name: str, value: Any) -> str | None:
    if value is None:
        return None
    return _normalize_logical_id(field_name, value)


def _authorized_arguments(
    tool_name: str,
    arguments: Mapping[str, Any],
    request: LogicalAgentRequest,
) -> dict[str, str]:
    expected = _TOOL_ARGUMENTS.get(tool_name)
    if expected is None or set(arguments) != set(expected):
        raise Phase6AgentError("tool_arguments_invalid", "工具参数字段不符合受控契约。")
    normalized = {
        field: _normalize_logical_id(field, arguments[field]) for field in expected
    }
    authorized = request.tool_context()
    for field, value in normalized.items():
        if authorized.get(field) != value:
            raise Phase6AgentError(
                "tool_resource_not_authorized",
                f"工具请求的 {field} 不在本次运行授权范围内。",
            )
    return normalized


async def _call_backend(
    backend: ResearchToolBackend, method_name: str, *arguments: str
) -> dict[str, Any]:
    method = getattr(backend, method_name, None)
    if not callable(method):
        raise Phase6AgentError(
            "tool_backend_invalid", f"受控后端未实现 {method_name}。"
        )
    # Registered backends are commonly synchronous. Run those off the event loop so
    # the SDK's per-tool timeout and the outer run timeout remain enforceable, while
    # awaiting native async handlers directly to preserve cancellation semantics.
    if inspect.iscoroutinefunction(method):
        value = await method(*arguments)
    else:
        value = await asyncio.to_thread(method, *arguments)
    if inspect.isawaitable(value):
        value = await value
    if not isinstance(value, Mapping):
        raise Phase6AgentError(
            "tool_result_invalid", "受控工具后端必须返回 JSON 对象。"
        )
    try:
        payload = json.loads(
            json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)
        )
    except (TypeError, ValueError) as exc:
        raise Phase6AgentError(
            "tool_result_invalid", "受控工具后端返回的对象不可安全序列化。"
        ) from exc
    _reject_row_level_result(payload)
    return payload


def _reject_row_level_result(value: Any) -> None:
    if isinstance(value, Mapping):
        for key, nested in value.items():
            if (
                str(key).lower() in _FORBIDDEN_RESULT_KEYS
                or _FORBIDDEN_RESULT_KEY.fullmatch(str(key))
            ):
                raise Phase6AgentError(
                    "tool_result_policy_denied",
                    "受控工具结果包含路径、CSV 或行级数据字段。",
                )
            _reject_row_level_result(nested)
    elif isinstance(value, list):
        for nested in value:
            _reject_row_level_result(nested)
    elif isinstance(value, str) and (
        _ROW_IDENTIFIER_VALUE.search(value)
        or _ABSOLUTE_RESULT_PATH.search(value.strip())
    ):
        raise Phase6AgentError(
            "tool_result_policy_denied",
            "受控工具结果包含路径、CSV 或行级数据值。",
        )


def _json_result(value: Mapping[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)


def _safe_tool_failure(context: Any, error: Exception) -> str:
    """Return a model-visible stable code without exception text or traceback."""

    del context
    candidate = getattr(error, "code", None)
    code = (
        candidate
        if isinstance(candidate, str) and _SAFE_ERROR_CODE.fullmatch(candidate)
        else "tool_execution_failed"
    )
    return json.dumps(
        {"status": "error", "error_code": code},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _safe_tool_timeout(context: Any, error: Exception) -> str:
    """Return a stable timeout result without the SDK's free-text exception body."""

    del context, error
    return json.dumps(
        {"status": "error", "error_code": "tool_timeout"},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _record_result(
    result: Any,
    *,
    model: str,
    latency_ms: float,
    tracing_disabled: bool,
    provider: str = "openai",
    transport: str = "openai_responses",
) -> AgentRunRecord:
    interruptions = _extract_interruptions(result)
    tool_calls = _extract_tool_calls(result, interruptions)
    final_output = _normalize_final_output(getattr(result, "final_output", None))
    return AgentRunRecord(
        status="waiting_approval" if interruptions else "completed",
        model=model,
        final_output=None if interruptions else final_output,
        tool_calls=tool_calls,
        usage=_extract_usage(result),
        latency_ms=latency_ms,
        cost_usd=None,
        approval_interruptions=interruptions,
        tracing_disabled=tracing_disabled,
        model_responses=_extract_model_responses(result),
        tool_observations=_extract_tool_observations(result, tool_calls),
        provider=provider,
        transport=transport,
    )


def _extract_interruptions(result: Any) -> tuple[ApprovalInterruption, ...]:
    builders: list[dict[str, Any]] = []
    by_call_id: dict[str, list[dict[str, Any]]] = {}
    for item in getattr(result, "interruptions", ()) or ():
        name = _tool_name(item)
        call_id = _call_id(item)
        arguments, arguments_valid = _normalize_recorded_arguments(
            name, _tool_arguments(item)
        )
        if call_id is None:
            status = "invalid_missing_call_id"
        elif name not in _TOOL_ARGUMENTS:
            status = "invalid_unknown_tool"
        elif not arguments_valid:
            status = "arguments_invalid"
        else:
            status = "awaiting_approval"
        builder = {
            "call_id": call_id,
            "name": name,
            "arguments": arguments,
            "status": status,
        }
        builders.append(builder)
        if call_id is not None:
            matches = by_call_id.setdefault(call_id, [])
            matches.append(builder)
            if len(matches) > 1:
                for duplicate in matches:
                    duplicate["status"] = "invalid_duplicate_call_id"
    return tuple(
        ApprovalInterruption(
            call_id=item["call_id"],
            name=item["name"],
            arguments=dict(item["arguments"]),
            status=item["status"],
        )
        for item in builders
    )


def _extract_tool_calls(
    result: Any, interruptions: tuple[ApprovalInterruption, ...]
) -> tuple[AgentToolCall, ...]:
    calls: list[dict[str, Any]] = []
    by_call_id: dict[str, list[dict[str, Any]]] = {}
    for item in getattr(result, "new_items", ()) or ():
        item_type = getattr(item, "type", None)
        if item_type == "tool_call_item":
            name = _tool_name(item)
            call_id = _call_id(item)
            arguments, arguments_valid = _normalize_recorded_arguments(
                name, _tool_arguments(item)
            )
            if call_id is None:
                status = "invalid_missing_call_id"
            elif name not in _TOOL_ARGUMENTS:
                status = "invalid_unknown_tool"
            elif not arguments_valid:
                status = "arguments_invalid"
            else:
                status = "requested"
            call = {
                "call_id": call_id,
                "name": name,
                "arguments": arguments,
                "status": status,
            }
            calls.append(call)
            if call_id is not None:
                matches = by_call_id.setdefault(call_id, [])
                matches.append(call)
                if len(matches) > 1:
                    for duplicate in matches:
                        duplicate["status"] = "invalid_duplicate_call_id"
        elif item_type == "tool_call_output_item":
            call_id = _call_id(item)
            matches = by_call_id.get(call_id, []) if call_id is not None else []
            if call_id is None:
                calls.append(
                    {
                        "call_id": None,
                        "name": _tool_name(item),
                        "arguments": {},
                        "status": "invalid_missing_call_id",
                    }
                )
            elif not matches:
                calls.append(
                    {
                        "call_id": call_id,
                        "name": _tool_name(item),
                        "arguments": {},
                        "status": "invalid_dangling_output",
                    }
                )
            elif len(matches) > 1:
                for duplicate in matches:
                    duplicate["status"] = "invalid_duplicate_call_id"
            elif matches[0]["status"] not in {
                "arguments_invalid",
                "invalid_missing_call_id",
                "invalid_unknown_tool",
                "invalid_duplicate_call_id",
            }:
                matches[0]["status"] = _tool_output_status(
                    getattr(item, "output", None)
                )

    for interruption in interruptions:
        matches = (
            by_call_id.get(interruption.call_id, [])
            if interruption.call_id is not None
            else []
        )
        if len(matches) == 1:
            matches[0]["status"] = interruption.status
        elif len(matches) > 1:
            for duplicate in matches:
                duplicate["status"] = "invalid_duplicate_call_id"
        else:
            calls.append(
                {
                "call_id": interruption.call_id,
                "name": interruption.name,
                "arguments": dict(interruption.arguments),
                    "status": interruption.status,
                }
            )
    return tuple(
        AgentToolCall(
            call_id=item["call_id"],
            name=item["name"],
            arguments=dict(item["arguments"]),
            status=item["status"],
        )
        for item in calls
    )


def _tool_output_status(output: Any) -> str:
    candidate = output
    if isinstance(candidate, str):
        try:
            candidate = json.loads(candidate)
        except (json.JSONDecodeError, TypeError):
            return "invalid_unstructured_output"
    if not isinstance(candidate, Mapping):
        return "invalid_unstructured_output"
    if isinstance(candidate, Mapping) and str(candidate.get("status", "")).lower() in {
        "error",
        "failed",
    }:
        return "failed"
    return "succeeded"


def _extract_tool_observations(
    result: Any, calls: tuple[AgentToolCall, ...]
) -> tuple[AgentToolObservation, ...]:
    names_by_call_id = {
        call.call_id: call.name
        for call in calls
        if call.call_id is not None
    }
    output: list[AgentToolObservation] = []
    for item in getattr(result, "new_items", ()) or ():
        if getattr(item, "type", None) != "tool_call_output_item":
            continue
        call_id = _call_id(item)
        name = names_by_call_id.get(call_id, _tool_name(item))
        raw_output = getattr(item, "output", None)
        status = _tool_output_status(raw_output)
        payload = _parse_tool_output_payload(raw_output)
        error_code = None
        if isinstance(payload, Mapping):
            candidate = payload.get("error_code")
            if isinstance(candidate, str) and _SAFE_ERROR_CODE.fullmatch(candidate):
                error_code = candidate
        evidence_ids = (
            tuple(sorted(_collect_evidence_ids(payload)))
            if name == "read_aggregate_evidence" and status == "succeeded"
            else ()
        )
        output.append(
            AgentToolObservation(
                call_id=call_id,
                name=name,
                status=status,
                evidence_ids=evidence_ids,
                error_code=error_code,
                output_sha256=_hash_untrusted_arguments(raw_output),
            )
        )
    return tuple(output)


def _parse_tool_output_payload(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return None


def _collect_evidence_ids(value: Any) -> set[str]:
    found: set[str] = set()
    if isinstance(value, Mapping):
        for key, nested in value.items():
            if str(key) == "evidence_id" and isinstance(nested, str):
                if _EVIDENCE_ID.fullmatch(nested):
                    found.add(nested)
            elif str(key) == "evidence_ids" and isinstance(nested, list):
                for item in nested:
                    if isinstance(item, str) and _EVIDENCE_ID.fullmatch(item):
                        found.add(item)
            else:
                found.update(_collect_evidence_ids(nested))
    elif isinstance(value, list):
        for nested in value:
            found.update(_collect_evidence_ids(nested))
    return found


def _tool_name(item: Any) -> str:
    for attribute in ("name", "tool_name"):
        value = getattr(item, attribute, None)
        if isinstance(value, str):
            return _safe_trace_text(value, missing="<missing-tool>")
    raw = getattr(item, "raw_item", None)
    value = _raw_value(raw, "name") or _raw_value(raw, "tool_name")
    return _safe_trace_text(value, missing="<missing-tool>")


def _tool_arguments(item: Any) -> Any:
    value = getattr(item, "arguments", None)
    if value is not None:
        return value
    raw = getattr(item, "raw_item", None)
    for field in ("arguments", "params", "input"):
        candidate = _raw_value(raw, field)
        if candidate is not None:
            return candidate
    return None


def _call_id(item: Any) -> str | None:
    value = getattr(item, "call_id", None)
    if value is None:
        raw = getattr(item, "raw_item", None)
        value = _raw_value(raw, "call_id") or _raw_value(raw, "id")
    return _safe_trace_text(value, missing="") or None


def _raw_value(raw: Any, field: str) -> Any:
    if isinstance(raw, Mapping):
        return raw.get(field)
    return getattr(raw, field, None)


def _normalize_recorded_arguments(
    tool_name: str, raw_arguments: Any
) -> tuple[dict[str, str], bool]:
    if tool_name not in _TOOL_ARGUMENTS:
        return {"arguments_sha256": _hash_untrusted_arguments(raw_arguments)}, False
    try:
        parsed = json.loads(raw_arguments) if isinstance(raw_arguments, str) else raw_arguments
        expected = _TOOL_ARGUMENTS[tool_name]
        if not isinstance(parsed, Mapping) or set(parsed) != set(expected):
            raise ValueError("invalid fields")
        normalized = {
            field: _normalize_logical_id(field, parsed[field]) for field in expected
        }
        return normalized, True
    except (KeyError, TypeError, ValueError, json.JSONDecodeError, Phase6AgentError):
        # Never preserve rejected raw values: they could contain a path, CSV, or row data.
        return {}, False


def _hash_untrusted_arguments(value: Any) -> str:
    try:
        parsed = json.loads(value) if isinstance(value, str) else value
        canonical = json.dumps(
            parsed, ensure_ascii=False, sort_keys=True, allow_nan=False, separators=(",", ":")
        )
    except (TypeError, ValueError, json.JSONDecodeError):
        canonical = f"{type(value).__name__}:{value}"
    return hashlib.sha256(canonical.encode("utf-8", errors="replace")).hexdigest()


def _safe_trace_text(value: Any, *, missing: str) -> str:
    if value is None:
        return missing
    text = str(value)
    printable = "".join(character if character.isprintable() else "?" for character in text)
    return printable[:128] or missing


def _extract_usage(result: Any) -> AgentUsage:
    wrapper = getattr(result, "context_wrapper", None)
    return _usage_from_object(getattr(wrapper, "usage", None))


def _extract_model_responses(result: Any) -> tuple[AgentModelResponse, ...]:
    output: list[AgentModelResponse] = []
    for index, response in enumerate(getattr(result, "raw_responses", ()) or ()):
        usage = getattr(response, "usage", None)
        output.append(
            AgentModelResponse(
                response_index=index,
                response_id_sha256=_optional_identifier_hash(
                    getattr(response, "response_id", None)
                ),
                request_id_sha256=_optional_identifier_hash(
                    getattr(response, "request_id", None)
                ),
                usage=_usage_from_object(usage),
                request_usages=_request_usage_entries(usage),
            )
        )
    return tuple(output)


def _usage_from_object(usage: Any) -> AgentUsage:
    if usage is None:
        return AgentUsage(None, None, None, None, None, False)
    requests = _optional_nonnegative_int(getattr(usage, "requests", None))
    input_tokens = _optional_nonnegative_int(getattr(usage, "input_tokens", None))
    output_tokens = _optional_nonnegative_int(getattr(usage, "output_tokens", None))
    total_tokens = _optional_nonnegative_int(getattr(usage, "total_tokens", None))
    details = getattr(usage, "input_tokens_details", None)
    cached = getattr(details, "cached_tokens", None)
    return AgentUsage(
        requests=requests,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=total_tokens,
        cached_input_tokens=(
            _optional_nonnegative_int(cached) if cached is not None else None
        ),
        complete=all(
            value is not None
            for value in (requests, input_tokens, output_tokens, total_tokens)
        ),
    )


def _request_usage_entries(usage: Any) -> tuple[AgentRequestUsage, ...]:
    output: list[AgentRequestUsage] = []
    for index, entry in enumerate(
        getattr(usage, "request_usage_entries", ()) or ()
    ):
        input_tokens = _optional_nonnegative_int(
            getattr(entry, "input_tokens", None)
        )
        output_tokens = _optional_nonnegative_int(
            getattr(entry, "output_tokens", None)
        )
        total_tokens = _optional_nonnegative_int(
            getattr(entry, "total_tokens", None)
        )
        input_details = getattr(entry, "input_tokens_details", None)
        output_details = getattr(entry, "output_tokens_details", None)
        output.append(
            AgentRequestUsage(
                request_index=index,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                total_tokens=total_tokens,
                cached_input_tokens=_optional_nonnegative_int(
                    getattr(input_details, "cached_tokens", None)
                ),
                cache_write_tokens=_optional_nonnegative_int(
                    getattr(input_details, "cache_write_tokens", None)
                ),
                reasoning_tokens=_optional_nonnegative_int(
                    getattr(output_details, "reasoning_tokens", None)
                ),
                complete=all(
                    value is not None
                    for value in (input_tokens, output_tokens, total_tokens)
                ),
            )
        )
    return tuple(output)


def _optional_identifier_hash(value: Any) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    return hashlib.sha256(value.encode("utf-8", errors="replace")).hexdigest()


def _optional_nonnegative_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


def _normalize_final_output(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)
    except (TypeError, ValueError):
        return str(value)
