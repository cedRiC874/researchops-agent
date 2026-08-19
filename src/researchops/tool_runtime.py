from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
import time
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Any

from .audit import AuditError, AuditLedger, canonical_json, safe_audit_value, sha256_json
from .artifact_security import ArtifactPermissionError, enable_parent_acl_inheritance
from .contracts import ResearchDesign
from .data_quality import profile_csv
from .method_selection import recommend_method


POLICY_VERSION = "1.0.0"
_RELEASE_SLUG = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,62})$")


class RiskLevel(str, Enum):
    READ_ONLY = "read_only"
    CONTROLLED_WRITE = "controlled_write"
    SENSITIVE_EXPORT = "sensitive_export"
    EXTERNAL_ACTION = "external_action"
    DESTRUCTIVE = "destructive"
    ARBITRARY_EXECUTION = "arbitrary_execution"


class PolicyDisposition(str, Enum):
    ALLOW = "allow"
    REQUIRE_APPROVAL = "require_approval"
    DENY = "deny"


class IdempotencyMode(str, Enum):
    NONE = "none"
    IDEMPOTENT = "idempotent"
    RECONCILABLE = "reconcilable"


class ToolRuntimeError(RuntimeError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        retryable: bool = False,
        outcome_unknown: bool = False,
        cause_code: str | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable
        self.outcome_unknown = outcome_unknown
        self.cause_code = cause_code

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": "error",
            "error_code": self.code,
            "message": str(self),
            "retryable": self.retryable,
            "outcome_unknown": self.outcome_unknown,
            "cause_code": self.cause_code,
        }


class TransientToolError(ToolRuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(code, message, retryable=True)


class PermanentToolError(ToolRuntimeError):
    pass


class AmbiguousToolOutcome(ToolRuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(code, message, outcome_unknown=True)


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int = 1
    initial_backoff_ms: int = 250
    multiplier: float = 2.0
    max_backoff_ms: int = 5_000

    def delay_ms(self, completed_attempts: int) -> int:
        delay = self.initial_backoff_ms * (self.multiplier ** max(completed_attempts - 1, 0))
        return min(int(delay), self.max_backoff_ms)


@dataclass(frozen=True)
class ToolInvocationContext:
    run_id: str
    call_id: str
    attempt_no: int
    idempotency_key: str


ToolHandler = Callable[[Mapping[str, Any], ToolInvocationContext], Mapping[str, Any]]
ArgumentValidator = Callable[[Mapping[str, Any]], dict[str, Any]]
SafeProjector = Callable[[Mapping[str, Any]], dict[str, Any]]
ScopeBuilder = Callable[[Mapping[str, Any]], dict[str, Any]]


@dataclass(frozen=True)
class ToolSpec:
    name: str
    version: str
    risk: RiskLevel
    handler: ToolHandler
    validate_arguments: ArgumentValidator
    safe_arguments: SafeProjector
    safe_result: SafeProjector
    scope_resources: ScopeBuilder
    retry_policy: RetryPolicy = RetryPolicy()
    idempotency: IdempotencyMode = IdempotencyMode.NONE
    policy_version: str = POLICY_VERSION


@dataclass(frozen=True)
class ToolCallOutcome:
    call_id: str
    run_id: str
    tool_name: str
    status: str
    requires_approval: bool
    result: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "call_id": self.call_id,
            "run_id": self.run_id,
            "tool_name": self.tool_name,
            "status": self.status,
            "requires_approval": self.requires_approval,
            "result": self.result,
        }


class ToolPolicy:
    def __init__(self) -> None:
        self._rules = {
            RiskLevel.READ_ONLY: PolicyDisposition.ALLOW,
            RiskLevel.CONTROLLED_WRITE: PolicyDisposition.REQUIRE_APPROVAL,
            RiskLevel.SENSITIVE_EXPORT: PolicyDisposition.DENY,
            RiskLevel.EXTERNAL_ACTION: PolicyDisposition.REQUIRE_APPROVAL,
            RiskLevel.DESTRUCTIVE: PolicyDisposition.REQUIRE_APPROVAL,
            RiskLevel.ARBITRARY_EXECUTION: PolicyDisposition.DENY,
        }

    def evaluate(self, risk: RiskLevel) -> PolicyDisposition:
        disposition = self._rules.get(risk)
        if disposition is None:
            raise ToolRuntimeError("tool_policy_denied", "未知风险类别被默认拒绝。")
        return disposition


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, ToolSpec] = {}

    def register(self, spec: ToolSpec) -> None:
        if spec.name in self._tools:
            raise ValueError(f"工具已注册：{spec.name}")
        if spec.retry_policy.max_attempts < 1:
            raise ValueError("max_attempts 必须至少为 1。")
        if (
            spec.risk is not RiskLevel.READ_ONLY
            and spec.retry_policy.max_attempts > 1
            and spec.idempotency is IdempotencyMode.NONE
        ):
            raise ValueError("有副作用的工具只有在幂等或可核对时才能自动重试。")
        self._tools[spec.name] = spec

    def get(self, name: str) -> ToolSpec:
        try:
            return self._tools[name]
        except KeyError as exc:
            raise ToolRuntimeError("tool_unknown", f"工具不在允许列表中：{name}") from exc

    def specs(self) -> tuple[ToolSpec, ...]:
        return tuple(self._tools[name] for name in sorted(self._tools))


class ControlledToolExecutor:
    def __init__(
        self,
        ledger: AuditLedger,
        registry: ToolRegistry,
        *,
        policy: ToolPolicy | None = None,
        sleeper: Callable[[float], None] = time.sleep,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.ledger = ledger
        self.registry = registry
        self.policy = policy or ToolPolicy()
        self._sleeper = sleeper
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def propose(
        self,
        run_id: str,
        tool_name: str,
        arguments: Mapping[str, Any],
        *,
        call_id: str | None = None,
    ) -> ToolCallOutcome:
        try:
            spec = self.registry.get(tool_name)
        except ToolRuntimeError as exc:
            self.ledger.append_event(
                run_id,
                "tool_policy_denied",
                {"tool_name": tool_name, "error_code": exc.code},
                actor_kind="policy",
            )
            raise
        disposition = self.policy.evaluate(spec.risk)
        if disposition is PolicyDisposition.DENY:
            self.ledger.append_event(
                run_id,
                "tool_policy_denied",
                {
                    "tool_name": tool_name,
                    "risk_class": spec.risk.value,
                    "policy_version": spec.policy_version,
                    "error_code": "tool_policy_denied",
                },
                actor_kind="policy",
            )
            raise ToolRuntimeError("tool_policy_denied", "该风险类别在当前阶段被策略禁止。")

        try:
            normalized = self._validate(spec, arguments)
        except ToolRuntimeError as exc:
            self.ledger.append_event(
                run_id,
                "tool_arguments_rejected",
                {"tool_name": tool_name, "error_code": exc.code},
                actor_kind="policy",
            )
            raise
        identifier = call_id or f"CALL-{uuid.uuid4().hex[:16].upper()}"
        args_hash = sha256_json(normalized)
        safe_args = spec.safe_arguments(normalized)
        scope_hash = self._scope_hash(identifier, spec, normalized)
        requires_approval = disposition is PolicyDisposition.REQUIRE_APPROVAL
        self.ledger.create_tool_call(
            call_id=identifier,
            run_id=run_id,
            tool_name=spec.name,
            tool_version=spec.version,
            policy_version=spec.policy_version,
            risk_class=spec.risk.value,
            safe_args=safe_args,
            args_hash=args_hash,
            approval_scope_hash=scope_hash,
            requires_approval=requires_approval,
        )
        if requires_approval:
            return ToolCallOutcome(
                call_id=identifier,
                run_id=run_id,
                tool_name=spec.name,
                status="awaiting_approval",
                requires_approval=True,
            )
        return self.execute(identifier, arguments=normalized)

    def decide(
        self,
        call_id: str,
        *,
        decision: str,
        approver: str,
        reason: str | None = None,
        expires_in_seconds: int = 900,
    ) -> ToolCallOutcome:
        call = self.ledger.get_tool_call(call_id)
        spec, normalized = self._revalidate_call(call)
        scope_hash = self._scope_hash(call_id, spec, normalized)
        if not approver.strip():
            raise ToolRuntimeError("tool_approver_invalid", "审批者标识不能为空。")
        expires_at = None
        if decision == "approve":
            if expires_in_seconds <= 0:
                raise ToolRuntimeError("tool_approval_expiry_invalid", "审批有效期必须大于 0。")
            expires_at = (
                self._utc_now() + timedelta(seconds=expires_in_seconds)
            ).isoformat()
        try:
            self.ledger.record_approval(
                call_id,
                approval_scope_hash=scope_hash,
                decision=decision,
                approver=approver,
                reason=reason,
                expires_at_utc=expires_at,
            )
        except AuditError as exc:
            raise ToolRuntimeError(exc.code, str(exc)) from exc
        return ToolCallOutcome(
            call_id=call_id,
            run_id=str(call["run_id"]),
            tool_name=str(call["tool_name"]),
            status="approved" if decision == "approve" else "rejected",
            requires_approval=True,
        )

    def execute(
        self,
        call_id: str,
        *,
        arguments: Mapping[str, Any] | None = None,
    ) -> ToolCallOutcome:
        call = self.ledger.get_tool_call(call_id)
        terminal = str(call["status"])
        if terminal == "succeeded":
            self.ledger.append_event(
                str(call["run_id"]),
                "tool_call_replayed",
                {
                    "call_id": call_id,
                    "tool_name": call["tool_name"],
                    "handler_invoked": False,
                    "result_hash": call["result_hash"],
                },
                actor_kind="tool_runtime",
            )
            return ToolCallOutcome(
                call_id=call_id,
                run_id=str(call["run_id"]),
                tool_name=str(call["tool_name"]),
                status="succeeded",
                requires_approval=call["risk_class"] != RiskLevel.READ_ONLY.value,
                result=call["safe_result"],
            )
        terminal_errors = {
            "rejected": "tool_approval_rejected",
            "expired": "tool_approval_expired",
            "failed": str(call["terminal_error_code"] or "tool_permanent_failure"),
            "outcome_unknown": "tool_outcome_unknown",
        }
        if terminal in terminal_errors:
            self.ledger.append_event(
                str(call["run_id"]),
                "tool_execution_blocked",
                {
                    "call_id": call_id,
                    "tool_name": call["tool_name"],
                    "error_code": terminal_errors[terminal],
                },
                actor_kind="policy",
            )
            raise ToolRuntimeError(terminal_errors[terminal], "工具调用已处于不可恢复的终态。")
        if terminal == "awaiting_approval":
            self.ledger.append_event(
                str(call["run_id"]),
                "tool_execution_blocked",
                {
                    "call_id": call_id,
                    "tool_name": call["tool_name"],
                    "error_code": "tool_approval_required",
                },
                actor_kind="policy",
            )
            raise ToolRuntimeError("tool_approval_required", "工具调用必须先由人工审批。")

        spec = self.registry.get(str(call["tool_name"]))
        candidate_arguments = arguments if arguments is not None else call["safe_args"]
        normalized = self._validate(spec, candidate_arguments)
        self._assert_call_identity(call, spec, normalized)
        disposition = self.policy.evaluate(spec.risk)
        if disposition is PolicyDisposition.DENY:
            self.ledger.fail_tool_call_without_attempt(call_id, "tool_policy_denied")
            raise ToolRuntimeError("tool_policy_denied", "工具风险策略已变更为拒绝。")
        if disposition is PolicyDisposition.REQUIRE_APPROVAL:
            self._assert_valid_approval(call_id, call)

        max_attempts = spec.retry_policy.max_attempts
        while True:
            try:
                attempt_no = self.ledger.start_attempt(call_id)
            except AuditError as exc:
                raise ToolRuntimeError(exc.code, str(exc)) from exc
            started = time.perf_counter()
            context = ToolInvocationContext(
                run_id=str(call["run_id"]),
                call_id=call_id,
                attempt_no=attempt_no,
                idempotency_key=str(call["idempotency_key"]),
            )
            try:
                raw_result = spec.handler(normalized, context)
                if not isinstance(raw_result, Mapping):
                    raise PermanentToolError(
                        "tool_result_not_serializable",
                        "受控工具必须返回 JSON 对象。",
                    )
                result_hash = sha256_json(raw_result)
                safe_result = spec.safe_result(raw_result)
                duration_ms = max((time.perf_counter() - started) * 1000.0, 0.0)
                self.ledger.finish_attempt_success(
                    call_id,
                    attempt_no,
                    duration_ms=duration_ms,
                    safe_result=safe_result,
                    result_hash=result_hash,
                )
                return ToolCallOutcome(
                    call_id=call_id,
                    run_id=str(call["run_id"]),
                    tool_name=spec.name,
                    status="succeeded",
                    requires_approval=disposition is PolicyDisposition.REQUIRE_APPROVAL,
                    result=safe_result,
                )
            except ToolRuntimeError as exc:
                error = exc
            except Exception as exc:  # Deliberately never store repr/traceback.
                error = PermanentToolError(
                    "tool_unhandled_error",
                    f"工具处理器返回未分类错误：{type(exc).__name__}",
                )

            duration_ms = max((time.perf_counter() - started) * 1000.0, 0.0)
            retry_capable = (
                spec.risk is RiskLevel.READ_ONLY
                or spec.idempotency in {IdempotencyMode.IDEMPOTENT, IdempotencyMode.RECONCILABLE}
            )
            will_retry = (
                error.retryable
                and not error.outcome_unknown
                and retry_capable
                and attempt_no < max_attempts
            )
            backoff_ms = spec.retry_policy.delay_ms(attempt_no) if will_retry else None
            exhausted = error.retryable and attempt_no >= max_attempts
            terminal_code = "tool_retry_exhausted" if exhausted else error.code
            self.ledger.finish_attempt_failure(
                call_id,
                attempt_no,
                duration_ms=duration_ms,
                error_code=error.code,
                safe_error_message=str(error),
                retryable=error.retryable,
                will_retry=will_retry,
                backoff_ms=backoff_ms,
                outcome_unknown=error.outcome_unknown,
                terminal_error_code=terminal_code,
            )
            if will_retry:
                self._sleeper(backoff_ms / 1000.0)
                continue
            if exhausted:
                raise ToolRuntimeError(
                    "tool_retry_exhausted",
                    "瞬时工具错误在允许的重试次数内未恢复。",
                    cause_code=error.code,
                ) from error
            raise error

    def _assert_valid_approval(self, call_id: str, call: Mapping[str, Any]) -> None:
        approval = self.ledger.get_approval(call_id)
        if approval is None:
            raise ToolRuntimeError("tool_approval_missing", "缺少人工审批记录。")
        if approval["decision"] != "approve":
            raise ToolRuntimeError("tool_approval_rejected", "人工已拒绝该调用。")
        if str(approval["approval_scope_hash"]) != str(call["approval_scope_hash"]):
            self.ledger.fail_tool_call_without_attempt(call_id, "tool_approval_mismatch")
            raise ToolRuntimeError("tool_approval_mismatch", "审批范围与调用身份不匹配。")
        expires_at = approval.get("expires_at_utc")
        if expires_at and datetime.fromisoformat(str(expires_at)) <= self._utc_now():
            self.ledger.expire_tool_call(call_id)
            raise ToolRuntimeError("tool_approval_expired", "人工审批已过期。")

    def _revalidate_call(self, call: Mapping[str, Any]) -> tuple[ToolSpec, dict[str, Any]]:
        spec = self.registry.get(str(call["tool_name"]))
        normalized = self._validate(spec, call["safe_args"])
        self._assert_call_identity(call, spec, normalized)
        return spec, normalized

    def _assert_call_identity(
        self, call: Mapping[str, Any], spec: ToolSpec, normalized: Mapping[str, Any]
    ) -> None:
        if spec.version != call["tool_version"] or spec.policy_version != call["policy_version"]:
            self.ledger.fail_tool_call_without_attempt(
                str(call["call_id"]), "tool_precondition_changed"
            )
            raise ToolRuntimeError("tool_precondition_changed", "工具或策略版本在审批后发生变化。")
        if sha256_json(normalized) != call["args_hash"]:
            self.ledger.fail_tool_call_without_attempt(
                str(call["call_id"]), "tool_approval_mismatch"
            )
            raise ToolRuntimeError("tool_approval_mismatch", "工具参数与提议时不一致。")
        current_scope = self._scope_hash(str(call["call_id"]), spec, normalized)
        if current_scope != call["approval_scope_hash"]:
            self.ledger.fail_tool_call_without_attempt(
                str(call["call_id"]), "tool_precondition_changed"
            )
            raise ToolRuntimeError(
                "tool_precondition_changed",
                "源产物、目标、工具或策略条件在审批后发生变化。",
            )

    def _scope_hash(
        self, call_id: str, spec: ToolSpec, normalized: Mapping[str, Any]
    ) -> str:
        resources = spec.scope_resources(normalized)
        return sha256_json(
            {
                "call_id": call_id,
                "tool_name": spec.name,
                "tool_version": spec.version,
                "policy_version": spec.policy_version,
                "normalized_args": normalized,
                "resources": resources,
            }
        )

    @staticmethod
    def _validate(spec: ToolSpec, arguments: Mapping[str, Any]) -> dict[str, Any]:
        if not isinstance(arguments, Mapping):
            raise ToolRuntimeError("tool_arguments_invalid", "工具参数必须是 JSON 对象。")
        try:
            normalized = spec.validate_arguments(arguments)
            canonical_json(normalized)
        except ToolRuntimeError:
            raise
        except (AuditError, TypeError, ValueError) as exc:
            raise ToolRuntimeError("tool_arguments_invalid", str(exc)) from exc
        return normalized

    def _utc_now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)


def build_project_tool_registry(project_root: str | Path) -> ToolRegistry:
    root = Path(project_root).resolve()
    registry = ToolRegistry()

    def validate_dataset_only(arguments: Mapping[str, Any]) -> dict[str, Any]:
        if set(arguments) != {"dataset_id"} or arguments.get("dataset_id") != "synthetic_trial":
            raise ToolRuntimeError(
                "tool_arguments_invalid",
                "dataset_id 当前只允许使用 synthetic_trial。",
            )
        return {"dataset_id": "synthetic_trial"}

    def dataset_path(arguments: Mapping[str, Any]) -> Path:
        if arguments.get("dataset_id") != "synthetic_trial":
            raise ToolRuntimeError("tool_arguments_invalid", "未知数据集逻辑 ID。")
        path = (root / "data" / "synthetic_trial.csv").resolve()
        data_root = (root / "data").resolve()
        if not path.is_relative_to(data_root) or not path.is_file():
            raise ToolRuntimeError("tool_source_not_found", "模拟数据集尚未生成。")
        return path

    def dataset_scope(arguments: Mapping[str, Any]) -> dict[str, Any]:
        return {"dataset_sha256": _sha256_file(dataset_path(arguments))}

    def inspect_dataset(
        arguments: Mapping[str, Any], context: ToolInvocationContext
    ) -> Mapping[str, Any]:
        del context
        profile = profile_csv(dataset_path(arguments))
        return {
            "dataset_id": arguments["dataset_id"],
            "dataset_sha256": profile.sha256,
            "row_count": profile.row_count,
            "column_count": profile.column_count,
            "duplicate_row_count": profile.duplicate_row_count,
            "rows_with_missing": profile.rows_with_missing,
            "complete_row_count": profile.complete_row_count,
            "columns": [
                {
                    "name": column.name,
                    "semantic_type": column.semantic_type,
                    "non_null_count": column.non_null_count,
                    "null_count": column.null_count,
                    "missing_rate": column.missing_rate,
                    "unique_count": column.unique_count,
                }
                for column in profile.columns
            ],
            "warnings": [
                {
                    "code": warning.code,
                    "severity": warning.severity,
                    "column": warning.column,
                }
                for warning in profile.warnings
            ],
            "sample_values_embedded": False,
        }

    registry.register(
        ToolSpec(
            name="inspect_dataset",
            version="1.0.0",
            risk=RiskLevel.READ_ONLY,
            handler=inspect_dataset,
            validate_arguments=validate_dataset_only,
            safe_arguments=lambda args: dict(args),
            safe_result=lambda result: dict(result),
            scope_resources=dataset_scope,
            retry_policy=RetryPolicy(max_attempts=3, initial_backoff_ms=100),
            idempotency=IdempotencyMode.IDEMPOTENT,
        )
    )

    def validate_method_request(arguments: Mapping[str, Any]) -> dict[str, Any]:
        if set(arguments) != {"dataset_id", "design_id"}:
            raise ToolRuntimeError(
                "tool_arguments_invalid",
                "方法推荐工具只接受 dataset_id 和 design_id。",
            )
        validate_dataset_only({"dataset_id": arguments.get("dataset_id")})
        design_id = arguments.get("design_id")
        if design_id not in {"trial_primary", "trial_unadjusted"}:
            raise ToolRuntimeError(
                "tool_arguments_invalid",
                "design_id 当前只允许 trial_primary 或 trial_unadjusted。",
            )
        return {"dataset_id": "synthetic_trial", "design_id": str(design_id)}

    def load_design(design_id: str) -> ResearchDesign:
        path = (root / "data" / "synthetic_trial_design.json").resolve()
        data_root = (root / "data").resolve()
        if not path.is_relative_to(data_root) or not path.is_file():
            raise ToolRuntimeError("tool_source_not_found", "模拟研究设计尚未生成。")
        payload = json.loads(path.read_text(encoding="utf-8"))
        if design_id == "trial_unadjusted":
            payload["covariates"] = []
            payload["covariate_timing"] = {}
            payload["analysis_population"] = "available_case"
            payload["normality"] = "reasonable"
        return ResearchDesign.from_dict(payload)

    def method_scope(arguments: Mapping[str, Any]) -> dict[str, Any]:
        design = load_design(str(arguments["design_id"]))
        return {
            **dataset_scope(arguments),
            "design_sha256": sha256_json(
                {
                    "question": design.question,
                    "objective": design.objective,
                    "outcome": design.outcome,
                    "predictor": design.predictor,
                    "covariates": list(design.covariates),
                    "reference_level": design.reference_level,
                    "contrast_level": design.contrast_level,
                }
            ),
        }

    def recommend_statistical_method(
        arguments: Mapping[str, Any], context: ToolInvocationContext
    ) -> Mapping[str, Any]:
        del context
        profile = profile_csv(dataset_path(arguments))
        recommendation = recommend_method(
            profile, load_design(str(arguments["design_id"]))
        )
        payload = recommendation.to_dict()
        return {
            "dataset_id": arguments["dataset_id"],
            "design_id": arguments["design_id"],
            "dataset_sha256": payload["dataset_sha256"],
            "status": payload["status"],
            "rule_version": payload["rule_version"],
            "primary_method": payload["primary_method"],
            "sensitivity_methods": payload["sensitivity_methods"],
            "rationale": payload["rationale"],
            "assumptions_to_check": payload["assumptions_to_check"],
            "required_diagnostics": payload["required_diagnostics"],
            "warnings": payload["warnings"],
        }

    registry.register(
        ToolSpec(
            name="recommend_statistical_method",
            version="1.0.0",
            risk=RiskLevel.READ_ONLY,
            handler=recommend_statistical_method,
            validate_arguments=validate_method_request,
            safe_arguments=lambda args: dict(args),
            safe_result=lambda result: dict(result),
            scope_resources=method_scope,
            retry_policy=RetryPolicy(max_attempts=3, initial_backoff_ms=100),
            idempotency=IdempotencyMode.IDEMPOTENT,
        )
    )

    def validate_bundle_only(arguments: Mapping[str, Any]) -> dict[str, Any]:
        if set(arguments) != {"bundle_id"} or arguments.get("bundle_id") != "phase3":
            raise ToolRuntimeError(
                "tool_arguments_invalid", "bundle_id 当前只允许使用 phase3。"
            )
        return {"bundle_id": "phase3"}

    def bundle_scope(arguments: Mapping[str, Any]) -> dict[str, Any]:
        bundle_path, chart_path = _bundle_paths(root, str(arguments["bundle_id"]))
        return {
            "source_bundle_sha256": _sha256_file(bundle_path),
            "source_chart_sha256": _sha256_file(chart_path),
        }

    def read_summary(
        arguments: Mapping[str, Any], context: ToolInvocationContext
    ) -> Mapping[str, Any]:
        bundle_path, _ = _bundle_paths(root, str(arguments["bundle_id"]))
        payload = json.loads(bundle_path.read_text(encoding="utf-8"))
        evidence = []
        for item in payload["evidence"]:
            sample_flow = item["sample_flow"]
            requested_population = payload.get("design", {}).get(
                "analysis_population"
            )
            realized_population = (
                "available_case"
                if sample_flow["included_rows"] < sample_flow["source_rows"]
                else requested_population
            )
            evidence.append(
                {
                    "evidence_id": item["evidence_id"],
                    "role": item["role"],
                    "method_code": item["method_code"],
                    "contrast": item["estimates"]["contrast"],
                    "p_value": item["test"]["p_value"],
                    "sample_flow": {
                        "source_rows": sample_flow["source_rows"],
                        "included_rows": sample_flow["included_rows"],
                        "excluded_rows": sample_flow["excluded_rows"],
                        "by_group": sample_flow["by_group"],
                    },
                    "missing_data_policy": item["input_spec"][
                        "missing_data_policy"
                    ],
                    "requested_population": requested_population,
                    "realized_population": realized_population,
                    "warnings": list(item.get("warnings", [])),
                }
            )
        return {
            "bundle_id": arguments["bundle_id"],
            "analysis_run_id": payload["run_id"],
            "dataset_sha256": payload["dataset"]["sha256"],
            "raw_data_embedded": bool(payload["dataset"]["raw_data_embedded"]),
            "evidence": evidence,
        }

    registry.register(
        ToolSpec(
            name="read_aggregate_evidence",
            version="1.0.0",
            risk=RiskLevel.READ_ONLY,
            handler=read_summary,
            validate_arguments=validate_bundle_only,
            safe_arguments=lambda args: dict(args),
            safe_result=lambda result: dict(result),
            scope_resources=bundle_scope,
            retry_policy=RetryPolicy(max_attempts=3, initial_backoff_ms=100),
            idempotency=IdempotencyMode.IDEMPOTENT,
        )
    )

    def validate_publish(arguments: Mapping[str, Any]) -> dict[str, Any]:
        if set(arguments) != {"bundle_id", "release_name"}:
            raise ToolRuntimeError(
                "tool_arguments_invalid", "发布工具只接受 bundle_id 和 release_name。"
            )
        if arguments.get("bundle_id") != "phase3":
            raise ToolRuntimeError("tool_arguments_invalid", "bundle_id 当前只允许 phase3。")
        release_name = str(arguments.get("release_name", ""))
        if not _RELEASE_SLUG.fullmatch(release_name):
            raise ToolRuntimeError(
                "tool_arguments_invalid",
                "release_name 只能包含小写字母、数字和连字符，最长 63 字符。",
            )
        return {"bundle_id": "phase3", "release_name": release_name}

    def publish_scope(arguments: Mapping[str, Any]) -> dict[str, Any]:
        result = bundle_scope(arguments)
        result["destination_resource_id"] = f"phase4-release:{arguments['release_name']}"
        return result

    def publish(
        arguments: Mapping[str, Any], context: ToolInvocationContext
    ) -> Mapping[str, Any]:
        bundle_path, chart_path = _bundle_paths(root, str(arguments["bundle_id"]))
        bundle_payload = json.loads(bundle_path.read_text(encoding="utf-8"))
        if bundle_payload.get("dataset", {}).get("raw_data_embedded") is not False:
            raise PermanentToolError(
                "tool_policy_denied", "只允许发布明确标记为不含原始数据的聚合证据。"
            )
        releases_root = (root / "artifacts" / "phase4" / "releases").resolve()
        releases_root.mkdir(parents=True, exist_ok=True)
        target = (releases_root / str(arguments["release_name"])).resolve()
        if not target.is_relative_to(releases_root):
            raise PermanentToolError("tool_policy_denied", "发布目标越过受控目录。")
        source_hashes = {
            "analysis_bundle.json": _sha256_file(bundle_path),
            "effect_estimates.png": _sha256_file(chart_path),
        }
        if target.exists():
            manifest_path = target / "release_manifest.json"
            if manifest_path.is_file():
                existing = json.loads(manifest_path.read_text(encoding="utf-8"))
                if (
                    existing.get("tool_call_id") == context.call_id
                    and existing.get("files") == source_hashes
                ):
                    return {
                        "release_id": f"phase4-release:{arguments['release_name']}",
                        "files": source_hashes,
                        "idempotent_replay": True,
                    }
            raise PermanentToolError(
                "tool_target_conflict", "目标发布名已存在且不属于同一次工具调用。"
            )

        staging = Path(
            tempfile.mkdtemp(prefix=f".{arguments['release_name']}-", dir=releases_root)
        ).resolve()
        try:
            shutil.copy2(bundle_path, staging / "analysis_bundle.json")
            shutil.copy2(chart_path, staging / "effect_estimates.png")
            manifest = {
                "schema_version": "1.0",
                "release_id": f"phase4-release:{arguments['release_name']}",
                "tool_call_id": context.call_id,
                "source_bundle_id": arguments["bundle_id"],
                "files": source_hashes,
                "raw_data_embedded": False,
            }
            (staging / "release_manifest.json").write_text(
                json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
                encoding="utf-8",
            )
            try:
                enable_parent_acl_inheritance(staging)
            except ArtifactPermissionError as exc:
                raise PermanentToolError(
                    "artifact_acl_inheritance_failed",
                    "无法让发布产物继承受控目录权限。",
                ) from exc
            if target.exists():
                raise PermanentToolError(
                    "tool_target_conflict", "发布期间目标被创建；未覆盖现有内容。"
                )
            os.replace(staging, target)
        except Exception:
            if staging.exists() and staging.is_relative_to(releases_root):
                shutil.rmtree(staging)
            raise
        return {
            "release_id": f"phase4-release:{arguments['release_name']}",
            "files": source_hashes,
            "idempotent_replay": False,
        }

    registry.register(
        ToolSpec(
            name="publish_aggregate_results",
            version="1.0.0",
            risk=RiskLevel.CONTROLLED_WRITE,
            handler=publish,
            validate_arguments=validate_publish,
            safe_arguments=lambda args: dict(args),
            safe_result=lambda result: dict(result),
            scope_resources=publish_scope,
            retry_policy=RetryPolicy(max_attempts=1),
            idempotency=IdempotencyMode.RECONCILABLE,
        )
    )
    return registry


def _bundle_paths(root: Path, bundle_id: str) -> tuple[Path, Path]:
    if bundle_id != "phase3":
        raise ToolRuntimeError("tool_arguments_invalid", "未知聚合证据包。")
    artifact_root = (root / "artifacts" / "phase3").resolve()
    bundle_path = (artifact_root / "analysis_bundle.json").resolve()
    chart_path = (artifact_root / "effect_estimates.png").resolve()
    if not bundle_path.is_file() or not chart_path.is_file():
        raise ToolRuntimeError("tool_source_not_found", "聚合证据包尚未生成。")
    return bundle_path, chart_path


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
