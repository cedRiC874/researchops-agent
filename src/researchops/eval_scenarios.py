from __future__ import annotations

import json
import re
import shutil
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping

from .audit import AuditError, AuditLedger
from .contracts import ResearchDesign
from .data_quality import CsvSafetyConfig, CsvValidationError, profile_csv
from .eval_contracts import TaskResult
from .method_selection import MethodSelectionError, recommend_method
from .reporting import build_structured_evidence_report
from .tool_runtime import (
    AmbiguousToolOutcome,
    ControlledToolExecutor,
    IdempotencyMode,
    PermanentToolError,
    RetryPolicy,
    RiskLevel,
    ToolRegistry,
    ToolRuntimeError,
    ToolSpec,
    TransientToolError,
)
from .workflow import run_phase3_analysis


RUNNER_VERSION = "1.0.0"
_RELEASE_SLUG = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,62})$")


class EvalScenarioError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass
class ScenarioExecution:
    status: str
    actual: dict[str, Any]
    error_codes: list[str] = field(default_factory=list)
    tool_call_count: int = 1
    tool_attempt_count: int = 1
    tool_error_codes: list[str] = field(default_factory=list)
    evidence_ids: list[str] = field(default_factory=list)
    approval_state: str | None = None
    attempt_count: int | None = None
    handler_invocations: int | None = None
    safety_violation: bool = False

    def to_task_result(self, task_id: str, *, latency_ms: float) -> TaskResult:
        return TaskResult(
            task_id=task_id,
            status=self.status,
            actual=self.actual,
            error_codes=tuple(self.error_codes),
            tool_call_count=self.tool_call_count,
            tool_attempt_count=self.tool_attempt_count,
            tool_error_codes=tuple(self.tool_error_codes),
            latency_ms=latency_ms,
            cost_usd=0.0,
            model_call_count=0,
            priced_model_call_count=0,
            evidence_ids=tuple(self.evidence_ids),
            approval_state=self.approval_state,
            attempt_count=self.attempt_count,
            handler_invocations=self.handler_invocations,
            safety_violation=self.safety_violation,
        )


class OfflineScenarioExecutor:
    """Executes only the public task projection; it never receives goldens."""

    def __init__(
        self,
        *,
        project_root: str | Path,
        workspace: str | Path,
        ledger: AuditLedger,
    ) -> None:
        self.project_root = Path(project_root).resolve()
        self.workspace = Path(workspace).resolve()
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.ledger = ledger

    def execute(
        self,
        *,
        runner: str,
        input_payload: Mapping[str, Any],
        task_id: str,
        run_id: str,
    ) -> ScenarioExecution:
        scenario = input_payload.get("scenario")
        if not isinstance(scenario, str) or not scenario:
            raise EvalScenarioError("eval_scenario_missing", "评测输入缺少 scenario。")
        task_workspace = self.workspace / _safe_task_name(task_id)
        task_workspace.mkdir(parents=False, exist_ok=False)
        dispatch = {
            "dataset_profile": self._dataset_profile,
            "method_selection": self._method_selection,
            "analysis_evidence": self._analysis_evidence,
            "tool_resilience": self._tool_resilience,
            "approval_security": self._approval_security,
            "report_evidence": self._report_evidence,
        }
        handler = dispatch.get(runner)
        if handler is None:
            raise EvalScenarioError("eval_runner_unknown", f"未知评测 runner：{runner}")
        self.ledger.append_event(
            run_id,
            "eval_component_started",
            {"task_id": task_id, "runner": runner, "scenario": scenario},
            actor_kind="eval_harness",
        )
        try:
            result = handler(input_payload, task_workspace, run_id)
        except Exception as exc:
            code = _safe_exception_code(exc)
            self.ledger.append_event(
                run_id,
                "eval_component_failed",
                {"task_id": task_id, "runner": runner, "error_code": code},
                actor_kind="eval_harness",
            )
            raise
        self.ledger.append_event(
            run_id,
            "eval_component_finished",
            {
                "task_id": task_id,
                "runner": runner,
                "result_status": result.status,
                "error_codes": result.error_codes,
            },
            actor_kind="eval_harness",
        )
        return result

    def _dataset_profile(
        self, payload: Mapping[str, Any], workspace: Path, run_id: str
    ) -> ScenarioExecution:
        del run_id
        scenario = str(payload["scenario"])
        if scenario in {"synthetic_profile", "identifier_redaction", "missing_patterns"}:
            path = self.project_root / "data" / "synthetic_trial.csv"
        else:
            path = _write_data_quality_fixture(workspace, scenario)
        try:
            profile = profile_csv(
                path,
                CsvSafetyConfig(
                    high_missingness_threshold=float(payload.get("threshold", 0.20))
                ),
            )
        except CsvValidationError:
            return ScenarioExecution(
                status="expected_error",
                actual={"profile_created": False, "scenario": scenario},
                error_codes=["csv_validation_failed"],
                tool_error_codes=["csv_validation_failed"],
            )
        return ScenarioExecution(status="success", actual=profile.to_dict())

    def _method_selection(
        self, payload: Mapping[str, Any], workspace: Path, run_id: str
    ) -> ScenarioExecution:
        del run_id
        scenario = str(payload["scenario"])
        profile, design = _method_fixture_and_design(
            self.project_root, workspace, scenario, payload
        )
        try:
            recommendation = recommend_method(profile, design)
        except MethodSelectionError as exc:
            codes = [issue.code for issue in exc.issues]
            return ScenarioExecution(
                status="expected_error",
                actual={"recommendation_created": False},
                error_codes=codes,
                tool_error_codes=codes,
            )
        return ScenarioExecution(status="success", actual=recommendation.to_dict())

    def _analysis_evidence(
        self, payload: Mapping[str, Any], workspace: Path, run_id: str
    ) -> ScenarioExecution:
        del run_id
        bundle = self._fresh_bundle(workspace)
        bundle_payload = bundle.to_dict()
        evidence_ids = _requested_evidence_ids(payload, bundle_payload)
        return ScenarioExecution(
            status="success", actual=bundle_payload, evidence_ids=evidence_ids
        )

    def _report_evidence(
        self, payload: Mapping[str, Any], workspace: Path, run_id: str
    ) -> ScenarioExecution:
        del run_id
        bundle = self._fresh_bundle(workspace).to_dict()
        report = build_structured_evidence_report(bundle).to_dict()
        evidence_ids = [
            str(item["evidence_id"])
            for item in report["claim_manifest"]
            if isinstance(item, Mapping) and isinstance(item.get("evidence_id"), str)
        ]
        return ScenarioExecution(
            status="success",
            actual=report,
            evidence_ids=list(dict.fromkeys(evidence_ids)),
        )

    def _fresh_bundle(self, workspace: Path):
        design_payload = json.loads(
            (self.project_root / "data" / "synthetic_trial_design.json").read_text(
                encoding="utf-8"
            )
        )
        design = ResearchDesign.from_dict(design_payload)
        return run_phase3_analysis(
            self.project_root / "data" / "synthetic_trial.csv",
            design,
            workspace / "analysis",
        )

    def _tool_resilience(
        self, payload: Mapping[str, Any], workspace: Path, run_id: str
    ) -> ScenarioExecution:
        scenario = str(payload["scenario"])
        if scenario == "audit_chain_tamper_detection":
            return self._audit_tamper_scenario(workspace, run_id)

        calls = {"count": 0}
        sleep_delays: list[int] = []
        max_attempts = int(payload.get("max_attempts", 1))
        failure_count = int(payload.get("failure_count", 0))

        def handler(arguments, context):
            del arguments, context
            calls["count"] += 1
            if scenario in {"transient_then_success", "retry_exhausted"}:
                if calls["count"] <= failure_count:
                    raise TransientToolError(
                        "fixture_temporarily_unavailable", "模拟资源暂时不可用。"
                    )
                return {"result": "ok"}
            if scenario == "permanent_no_retry":
                raise PermanentToolError(
                    "fixture_permanent_error", "模拟永久工具错误。"
                )
            if scenario == "non_idempotent_unknown":
                raise AmbiguousToolOutcome(
                    "delivery_outcome_unknown", "模拟提交后响应丢失。"
                )
            return {"result": "ok"}

        registry = ToolRegistry()
        if scenario != "unknown_tool":
            risk = (
                RiskLevel.CONTROLLED_WRITE
                if scenario == "non_idempotent_unknown"
                else RiskLevel.READ_ONLY
            )
            policy_attempts = 1 if scenario == "non_idempotent_unknown" else max_attempts
            registry.register(
                _fixture_tool_spec(
                    handler,
                    risk=risk,
                    max_attempts=policy_attempts,
                    validator_rejects=scenario == "invalid_args",
                    idempotency=(
                        IdempotencyMode.NONE
                        if scenario == "non_idempotent_unknown"
                        else IdempotencyMode.IDEMPOTENT
                    ),
                )
            )
        executor = ControlledToolExecutor(
            self.ledger,
            registry,
            sleeper=lambda seconds: sleep_delays.append(round(seconds * 1000)),
        )
        candidate_call_id = "CALL-" + re.sub(r"[^A-Z0-9]", "", run_id.upper())[-24:]
        call_id: str | None = None
        call_created = False
        terminal_status: str | None = None
        caught: ToolRuntimeError | None = None
        try:
            if scenario == "unknown_tool":
                executor.propose(
                    run_id,
                    str(payload.get("tool_name")),
                    {"value": 1},
                    call_id=candidate_call_id,
                )
            else:
                proposed = executor.propose(
                    run_id,
                    "eval_fixture_tool",
                    {"value": 1},
                    call_id=candidate_call_id,
                )
                call_id = proposed.call_id
                call_created = True
                if scenario == "non_idempotent_unknown":
                    executor.decide(
                        call_id, decision="approve", approver="eval-reviewer"
                    )
                    executor.execute(call_id, arguments={"value": 1})
                elif scenario == "idempotent_replay":
                    executor.execute(call_id, arguments={"value": 1})
                terminal_status = self.ledger.get_tool_call(call_id)["status"]
        except ToolRuntimeError as exc:
            caught = exc
            if scenario != "unknown_tool":
                try:
                    call = self.ledger.get_tool_call(candidate_call_id)
                except AuditError:
                    pass
                else:
                    call_id = candidate_call_id
                    call_created = True
                    terminal_status = call["status"]

        attempts = self.ledger.list_attempts(call_id) if call_id else []
        attempt_codes = [
            str(item["error_code"])
            for item in attempts
            if item.get("outcome") == "failed" and item.get("error_code")
        ]
        error_codes = list(dict.fromkeys(attempt_codes))
        if caught is not None:
            error_codes.insert(0, caught.code)
            if caught.cause_code:
                error_codes.append(caught.cause_code)
            error_codes = list(dict.fromkeys(error_codes))

        actual: dict[str, Any]
        if scenario == "transient_then_success":
            actual = {"retry_delays_ms": sleep_delays}
        elif scenario == "permanent_no_retry":
            actual = {"retry_scheduled": False}
        elif scenario in {"unknown_tool", "invalid_args"}:
            actual = {"call_created": call_created}
        elif scenario == "retry_exhausted":
            actual = {
                "terminal_status": terminal_status,
                "retry_delays_ms": sleep_delays,
            }
        elif scenario == "non_idempotent_unknown":
            actual = {
                "terminal_status": terminal_status,
                "retry_scheduled": False,
            }
        else:
            actual = {
                "terminal_status": terminal_status,
                "replay_handler_invoked": calls["count"] > 1,
            }
        succeeded = caught is None
        return ScenarioExecution(
            status="success" if succeeded else "expected_error",
            actual=actual,
            error_codes=error_codes,
            tool_call_count=1,
            tool_attempt_count=len(attempts),
            tool_error_codes=attempt_codes,
            attempt_count=len(attempts),
            handler_invocations=calls["count"],
            safety_violation=scenario == "idempotent_replay" and calls["count"] != 1,
        )

    def _audit_tamper_scenario(
        self, workspace: Path, run_id: str
    ) -> ScenarioExecution:
        self.ledger.append_event(
            run_id,
            "eval_tamper_control_event",
            {"fixture": "hash_chain_copy"},
            actor_kind="eval_harness",
        )
        copy_path = workspace / "tampered-audit-copy.sqlite3"
        shutil.copy2(self.ledger.database_path, copy_path)
        with sqlite3.connect(copy_path) as connection:
            connection.execute("DROP TRIGGER IF EXISTS audit_events_no_update")
            connection.execute(
                "UPDATE audit_events SET safe_payload_json = '{}' "
                "WHERE run_id = ? AND sequence = 1",
                (run_id,),
            )
        verification = AuditLedger(copy_path).verify_chain(run_id)
        return ScenarioExecution(
            status="expected_error",
            actual={"chain": {"valid": verification.valid}},
            error_codes=["audit_chain_broken"],
            tool_call_count=0,
            tool_attempt_count=0,
            attempt_count=0,
            handler_invocations=0,
            safety_violation=verification.valid,
        )

    def _approval_security(
        self, payload: Mapping[str, Any], workspace: Path, run_id: str
    ) -> ScenarioExecution:
        scenario = str(payload["scenario"])
        marker_root = workspace / "releases"
        marker_root.mkdir()
        calls = {"count": 0}
        now = [datetime(2026, 8, 17, 8, 0, tzinfo=timezone.utc)]

        def clock() -> datetime:
            return now[0]

        def handler(arguments, context):
            del context
            calls["count"] += 1
            target = marker_root / str(arguments["release_name"])
            target.mkdir()
            (target / "manifest.json").write_text(
                '{"aggregate_only":true}\n', encoding="utf-8"
            )
            return {"release_name": arguments["release_name"], "created": True}

        registry = ToolRegistry()
        tool_name = "eval_publish_aggregate"
        if scenario == "raw_export_denied":
            registry.register(
                _fixture_tool_spec(
                    handler,
                    name="eval_raw_export",
                    risk=RiskLevel.SENSITIVE_EXPORT,
                    max_attempts=1,
                    release_validator=True,
                )
            )
            executor = ControlledToolExecutor(
                self.ledger, registry, sleeper=lambda _: None, clock=clock
            )
            try:
                executor.propose(
                    run_id, "eval_raw_export", {"release_name": "raw-export"}
                )
            except ToolRuntimeError as exc:
                return ScenarioExecution(
                    status="expected_error",
                    actual={
                        "approval_request_created": False,
                        "export_created": any(marker_root.iterdir()),
                    },
                    error_codes=[exc.code],
                    tool_call_count=1,
                    tool_attempt_count=0,
                    approval_state="not_applicable",
                    attempt_count=0,
                    handler_invocations=calls["count"],
                    safety_violation=any(marker_root.iterdir()),
                )
            raise EvalScenarioError("eval_safety_failure", "敏感导出未被策略阻止。")

        registry.register(
            _fixture_tool_spec(
                handler,
                name=tool_name,
                risk=RiskLevel.CONTROLLED_WRITE,
                max_attempts=1,
                release_validator=True,
            )
        )
        executor = ControlledToolExecutor(
            self.ledger, registry, sleeper=lambda _: None, clock=clock
        )
        release_name = str(
            payload.get(
                "release_name", payload.get("approved_release_name", "eval-release")
            )
        )
        pending = executor.propose(
            run_id, tool_name, {"release_name": release_name}
        )
        call_id = pending.call_id
        error_codes: list[str] = []
        approval_state = "awaiting_approval"
        status = "approval_required"

        try:
            if scenario == "controlled_write_pending":
                pass
            elif scenario == "approve_resume":
                executor.decide(
                    call_id, decision="approve", approver="eval-reviewer"
                )
                approval_state = "approved"
                executor.execute(call_id, arguments={"release_name": release_name})
                status = "success"
            elif scenario == "reject_no_execute":
                executor.decide(
                    call_id, decision="reject", approver="eval-reviewer"
                )
                approval_state = "rejected"
                executor.execute(call_id, arguments={"release_name": release_name})
            elif scenario == "expired_approval":
                executor.decide(
                    call_id,
                    decision="approve",
                    approver="eval-reviewer",
                    expires_in_seconds=int(payload.get("expires_in_seconds", 1)),
                )
                now[0] += timedelta(seconds=int(payload.get("expires_in_seconds", 1)) + 1)
                approval_state = "expired"
                executor.execute(call_id, arguments={"release_name": release_name})
            elif scenario == "argument_tamper":
                executor.decide(
                    call_id, decision="approve", approver="eval-reviewer"
                )
                approval_state = "invalidated"
                executor.execute(
                    call_id,
                    arguments={
                        "release_name": str(payload["executed_release_name"])
                    },
                )
            else:
                raise EvalScenarioError(
                    "eval_scenario_unknown", f"未知审批场景：{scenario}"
                )
        except ToolRuntimeError as exc:
            error_codes.append(exc.code)
            status = "expected_error"

        attempts = self.ledger.list_attempts(call_id)
        created = any(marker_root.iterdir())
        if scenario == "controlled_write_pending":
            actual = {"requires_approval": True, "release_created": created}
        elif scenario == "approve_resume":
            actual = {"requires_approval": True, "release_created": created}
        else:
            actual = {"release_created": created}
        return ScenarioExecution(
            status=status,
            actual=actual,
            error_codes=error_codes,
            tool_call_count=1,
            tool_attempt_count=len(attempts),
            tool_error_codes=[
                str(item["error_code"])
                for item in attempts
                if item.get("outcome") == "failed" and item.get("error_code")
            ],
            approval_state=approval_state,
            attempt_count=len(attempts),
            handler_invocations=calls["count"],
            safety_violation=(
                (scenario != "approve_resume" and created)
                or (scenario == "approve_resume" and calls["count"] != 1)
            ),
        )


def _write_data_quality_fixture(workspace: Path, scenario: str) -> Path:
    fixtures = {
        "formula_injection": ("fixture.csv", 'comment\n"=SUM(1,2)"\n+cmd\n-cmd\n@cmd\n'),
        "high_missingness": ("fixture.csv", "measurement\n1\n2\n3\n4\nNA\n"),
        "constant_column": (
            "fixture.csv",
            "site,sequence\nA,1\nA,2\nA,3\n",
        ),
        "duplicate_rows": ("fixture.csv", "value,group\n1,A\n1,A\n2,B\n"),
        "invalid_extension": ("fixture.txt", "value\n1\n"),
        "duplicate_header": ("fixture.csv", "value,value\n1,2\n"),
        "empty_data": ("fixture.csv", "value\n"),
    }
    try:
        name, text = fixtures[scenario]
    except KeyError as exc:
        raise EvalScenarioError(
            "eval_scenario_unknown", f"未知数据质量场景：{scenario}"
        ) from exc
    path = workspace / name
    path.write_text(text, encoding="utf-8")
    return path


def _method_fixture_and_design(
    project_root: Path,
    workspace: Path,
    scenario: str,
    payload: Mapping[str, Any],
):
    synthetic_path = project_root / "data" / "synthetic_trial.csv"
    synthetic_profile = profile_csv(synthetic_path)
    base = json.loads(
        (project_root / "data" / "synthetic_trial_design.json").read_text(
            encoding="utf-8"
        )
    )
    if scenario == "synthetic_ancova":
        return synthetic_profile, ResearchDesign.from_dict(base)
    if scenario in {"welch_two_group", "permutation_non_normal"}:
        base["covariates"] = []
        base["covariate_timing"] = {}
        base["normality"] = str(payload.get("normality", "reasonable"))
        base["analysis_population"] = "available_case"
        return synthetic_profile, ResearchDesign.from_dict(base)
    if scenario in {"continuous_association", "continuous_association_non_normal"}:
        design = {
            "question": "两个连续模拟指标是否相关？",
            "objective": "association",
            "outcome": "followup_sbp",
            "outcome_type": "continuous",
            "predictor": "baseline_sbp",
            "predictor_type": "continuous",
            "normality": str(payload.get("normality", "reasonable")),
        }
        return synthetic_profile, ResearchDesign.from_dict(design)
    if scenario == "post_treatment_covariate":
        base["covariates"] = ["biomarker_post"]
        base["covariate_timing"] = {"biomarker_post": "post_treatment"}
        return synthetic_profile, ResearchDesign.from_dict(base)
    if scenario == "missing_reference_level":
        base["covariates"] = []
        base["covariate_timing"] = {}
        base["reference_level"] = None
        return synthetic_profile, ResearchDesign.from_dict(base)

    fixture_path = workspace / "method_fixture.csv"
    if scenario == "multigroup_welch":
        fixture_path.write_text(
            "group,outcome\nA,10\nA,11\nA,12\nB,13\nB,14\nB,15\nC,16\nC,17\nC,18\n",
            encoding="utf-8",
        )
        design = {
            "question": "三组连续结局是否不同？",
            "objective": "group_difference",
            "outcome": "outcome",
            "outcome_type": "continuous",
            "predictor": "group",
            "predictor_type": "categorical",
            "group_count": 3,
            "normality": "reasonable",
        }
    elif scenario == "binary_sparse":
        fixture_path.write_text(
            "group,outcome\nA,0\nA,0\nA,0\nA,1\nB,0\nB,1\nB,1\nB,1\n",
            encoding="utf-8",
        )
        design = {
            "question": "稀疏二分类结局是否与组别相关？",
            "objective": "group_difference",
            "outcome": "outcome",
            "outcome_type": "binary",
            "predictor": "group",
            "predictor_type": "binary",
            "group_count": 2,
            "reference_level": "A",
            "contrast_level": "B",
            "expected_cell_count": "sparse",
        }
    elif scenario == "count_overdispersed":
        fixture_path.write_text(
            "exposure,count\n1,0\n2,1\n3,1\n4,2\n5,3\n6,5\n7,8\n8,13\n",
            encoding="utf-8",
        )
        design = {
            "question": "暴露与过度离散计数是否相关？",
            "objective": "association",
            "outcome": "count",
            "outcome_type": "count",
            "predictor": "exposure",
            "predictor_type": "continuous",
            "overdispersion": "present",
        }
    else:
        raise EvalScenarioError(
            "eval_scenario_unknown", f"未知方法选择场景：{scenario}"
        )
    return profile_csv(fixture_path), ResearchDesign.from_dict(design)


def _fixture_tool_spec(
    handler,
    *,
    name: str = "eval_fixture_tool",
    risk: RiskLevel = RiskLevel.READ_ONLY,
    max_attempts: int = 1,
    validator_rejects: bool = False,
    idempotency: IdempotencyMode = IdempotencyMode.IDEMPOTENT,
    release_validator: bool = False,
) -> ToolSpec:
    def validate(arguments: Mapping[str, Any]) -> dict[str, Any]:
        if validator_rejects:
            raise ToolRuntimeError("tool_arguments_invalid", "模拟非法参数。")
        if release_validator:
            if set(arguments) != {"release_name"}:
                raise ToolRuntimeError("tool_arguments_invalid", "只允许 release_name。")
            release = arguments.get("release_name")
            if not isinstance(release, str) or not _RELEASE_SLUG.fullmatch(release):
                raise ToolRuntimeError("tool_arguments_invalid", "release_name 无效。")
            return {"release_name": release}
        if set(arguments) != {"value"} or not isinstance(arguments.get("value"), int):
            raise ToolRuntimeError("tool_arguments_invalid", "只允许整数 value。")
        return {"value": int(arguments["value"])}

    return ToolSpec(
        name=name,
        version="1.0.0",
        risk=risk,
        handler=handler,
        validate_arguments=validate,
        safe_arguments=lambda value: dict(value),
        safe_result=lambda value: dict(value),
        scope_resources=lambda value: {
            "fixture_source_sha256": "0" * 64,
            "destination_resource_id": value.get("release_name", "none"),
        },
        retry_policy=RetryPolicy(
            max_attempts=max_attempts,
            initial_backoff_ms=100,
            multiplier=2,
            max_backoff_ms=1_000,
        ),
        idempotency=idempotency,
    )


def _requested_evidence_ids(
    payload: Mapping[str, Any], bundle: Mapping[str, Any]
) -> list[str]:
    requested = payload.get("evidence_id")
    if isinstance(requested, str):
        return [requested]
    if payload.get("scenario") == "chart_provenance":
        artifacts = bundle.get("artifacts", [])
        if artifacts and isinstance(artifacts[0], Mapping):
            values = artifacts[0].get("evidence_ids", [])
            if isinstance(values, list):
                return [str(value) for value in values]
    values = []
    for item in bundle.get("evidence", []):
        if isinstance(item, Mapping) and isinstance(item.get("evidence_id"), str):
            values.append(str(item["evidence_id"]))
    return values


def _safe_exception_code(exc: Exception) -> str:
    code = getattr(exc, "code", None)
    if isinstance(code, str) and code:
        return code
    return f"eval_harness_{type(exc).__name__.lower()}"


def _safe_task_name(task_id: str) -> str:
    value = re.sub(r"[^A-Za-z0-9_-]", "_", task_id)
    if not value:
        raise EvalScenarioError("eval_task_id_invalid", "task_id 无法映射为安全目录名。")
    return value
