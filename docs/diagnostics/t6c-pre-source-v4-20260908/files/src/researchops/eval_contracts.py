from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping, Sequence


EVAL_SCHEMA_VERSION = "1.0"
EXPECTED_TASK_COUNT = 50
RUNNERS = frozenset(
    {
        "dataset_profile",
        "method_selection",
        "analysis_evidence",
        "tool_resilience",
        "approval_security",
        "report_evidence",
    }
)
CATEGORIES = frozenset(
    {
        "data_quality",
        "method_selection",
        "analysis_evidence",
        "tool_resilience",
        "approval_security",
        "report_evidence",
    }
)
EXPECTED_OUTCOMES = frozenset(
    {"success", "expected_error", "approval_required", "rejected"}
)

_TASK_FIELDS = frozenset(
    {
        "schema_version",
        "task_id",
        "category",
        "title",
        "runner",
        "input",
        "expected",
        "tags",
        "expected_outcome",
    }
)
_EXPECTED_FIELDS = frozenset(
    {
        "status",
        "error_code",
        "error_codes",
        "tool_error_codes",
        "exact",
        "numeric",
        "numeric_tolerance",
        "required_evidence",
        "required_evidence_ids",
        "approval_state",
        "attempt_count",
        "handler_invocations",
        "safety_violation",
    }
)
_NUMERIC_FIELDS = frozenset({"path", "value", "atol", "rtol"})
_RESULT_FIELDS = frozenset(
    {
        "task_id",
        "status",
        "actual",
        "error_codes",
        "tool_call_count",
        "tool_attempt_count",
        "tool_error_codes",
        "latency_ms",
        "cost_usd",
        "model_call_count",
        "priced_model_call_count",
        "evidence_ids",
        "approval_state",
        "attempt_count",
        "handler_invocations",
        "safety_violation",
    }
)


class EvalContractError(ValueError):
    """Stable, actionable error raised for an invalid evaluation artifact."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        line_number: int | None = None,
        task_id: str | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.line_number = line_number
        self.task_id = task_id

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": "error",
            "error_code": self.code,
            "message": str(self),
            "line_number": self.line_number,
            "task_id": self.task_id,
        }


@dataclass(frozen=True)
class NumericExpectation:
    path: str
    value: float
    atol: float = 0.0
    rtol: float = 0.0

    def __post_init__(self) -> None:
        _require_nonempty_string(self.path, "numeric.path")
        _require_finite_number(self.value, "numeric.value")
        _require_nonnegative_finite(self.atol, "numeric.atol")
        _require_nonnegative_finite(self.rtol, "numeric.rtol")

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "NumericExpectation":
        value = _require_mapping(payload, "numeric")
        _reject_unknown(value, _NUMERIC_FIELDS, "numeric")
        missing = {"path", "value"} - set(value)
        if missing:
            raise EvalContractError(
                "eval_missing_field",
                f"numeric 缺少字段：{', '.join(sorted(missing))}",
            )
        return cls(
            path=value["path"],
            value=value["value"],
            atol=value.get("atol", 0.0),
            rtol=value.get("rtol", 0.0),
        )


@dataclass(frozen=True)
class ExpectedResult:
    status: str | None = None
    error_codes: tuple[str, ...] = ()
    tool_error_codes: tuple[str, ...] = ()
    exact: Mapping[str, Any] = field(default_factory=dict)
    numeric: tuple[NumericExpectation, ...] = ()
    required_evidence_ids: tuple[str, ...] = ()
    approval_state: str | None = None
    attempt_count: int | None = None
    handler_invocations: int | None = None
    safety_violation: bool | None = None

    def __post_init__(self) -> None:
        if self.status is not None:
            _require_nonempty_string(self.status, "expected.status")
        _validate_unique_strings(self.error_codes, "expected.error_codes")
        _validate_strings(self.tool_error_codes, "expected.tool_error_codes")
        exact = _require_mapping(self.exact, "expected.exact")
        for path, expected_value in exact.items():
            _require_nonempty_string(path, "expected.exact path")
            _validate_json_value(expected_value, f"expected.exact[{path!r}]")
        _validate_unique_strings(
            self.required_evidence_ids, "expected.required_evidence_ids"
        )
        if self.approval_state is not None:
            _require_nonempty_string(
                self.approval_state, "expected.approval_state"
            )
        _require_optional_nonnegative_int(
            self.attempt_count, "expected.attempt_count"
        )
        _require_optional_nonnegative_int(
            self.handler_invocations, "expected.handler_invocations"
        )
        if self.safety_violation is not None and not isinstance(
            self.safety_violation, bool
        ):
            raise EvalContractError(
                "eval_invalid_type", "expected.safety_violation 必须是布尔值。"
            )
        object.__setattr__(self, "exact", MappingProxyType(dict(exact)))

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "ExpectedResult":
        value = _require_mapping(payload, "expected")
        _reject_unknown(value, _EXPECTED_FIELDS, "expected")

        if "error_code" in value and "error_codes" in value:
            raise EvalContractError(
                "eval_conflicting_fields",
                "expected.error_code 与 expected.error_codes 不能同时出现。",
            )
        error_codes: Sequence[Any]
        if "error_code" in value:
            error_codes = [value["error_code"]]
        else:
            error_codes = _require_sequence(
                value.get("error_codes", []), "expected.error_codes"
            )

        if "numeric" in value and "numeric_tolerance" in value:
            raise EvalContractError(
                "eval_conflicting_fields",
                "expected.numeric 与 expected.numeric_tolerance 不能同时出现。",
            )
        numeric_payload = value.get(
            "numeric", value.get("numeric_tolerance", [])
        )
        numeric_items = _require_sequence(numeric_payload, "expected.numeric")

        if "required_evidence" in value and "required_evidence_ids" in value:
            raise EvalContractError(
                "eval_conflicting_fields",
                "expected.required_evidence 与 required_evidence_ids 不能同时出现。",
            )
        evidence_payload = value.get(
            "required_evidence_ids", value.get("required_evidence", [])
        )
        evidence_ids = _require_sequence(
            evidence_payload, "expected.required_evidence_ids"
        )

        return cls(
            status=value.get("status"),
            error_codes=tuple(error_codes),
            tool_error_codes=tuple(
                _require_sequence(
                    value.get("tool_error_codes", []),
                    "expected.tool_error_codes",
                )
            ),
            exact=dict(_require_mapping(value.get("exact", {}), "expected.exact")),
            numeric=tuple(
                NumericExpectation.from_dict(item) for item in numeric_items
            ),
            required_evidence_ids=tuple(evidence_ids),
            approval_state=value.get("approval_state"),
            attempt_count=value.get("attempt_count"),
            handler_invocations=value.get("handler_invocations"),
            safety_violation=value.get("safety_violation"),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "error_codes": list(self.error_codes),
            "tool_error_codes": list(self.tool_error_codes),
            "exact": dict(self.exact),
            "numeric": [asdict(item) for item in self.numeric],
            "required_evidence_ids": list(self.required_evidence_ids),
            "approval_state": self.approval_state,
            "attempt_count": self.attempt_count,
            "handler_invocations": self.handler_invocations,
            "safety_violation": self.safety_violation,
        }


@dataclass(frozen=True)
class EvalTask:
    schema_version: str
    task_id: str
    category: str
    title: str
    runner: str
    input: Mapping[str, Any]
    expected: ExpectedResult
    tags: tuple[str, ...]
    expected_outcome: str

    def __post_init__(self) -> None:
        if self.schema_version != EVAL_SCHEMA_VERSION:
            raise EvalContractError(
                "eval_unknown_schema",
                f"不支持的评测 schema_version：{self.schema_version!r}",
                task_id=self.task_id or None,
            )
        _require_nonempty_string(self.task_id, "task_id")
        if self.category not in CATEGORIES:
            raise EvalContractError(
                "eval_unknown_category",
                f"不支持的 category：{self.category!r}",
                task_id=self.task_id,
            )
        _require_nonempty_string(self.title, "title")
        if self.runner not in RUNNERS:
            raise EvalContractError(
                "eval_unknown_runner",
                f"不支持的 runner：{self.runner!r}",
                task_id=self.task_id,
            )
        payload = _require_mapping(self.input, "input")
        _validate_json_value(payload, "input")
        _validate_unique_strings(self.tags, "tags")
        if self.expected_outcome not in EXPECTED_OUTCOMES:
            raise EvalContractError(
                "eval_unknown_expected_outcome",
                f"不支持的 expected_outcome：{self.expected_outcome!r}",
                task_id=self.task_id,
            )
        if self.expected_outcome == "expected_error" and not self.expected.error_codes:
            raise EvalContractError(
                "eval_missing_expected_error",
                "expected_outcome=expected_error 时必须提供 error_code(s)。",
                task_id=self.task_id,
            )
        if (
            self.expected_outcome == "approval_required"
            and self.expected.approval_state is None
        ):
            raise EvalContractError(
                "eval_missing_approval_state",
                "expected_outcome=approval_required 时必须提供 approval_state。",
                task_id=self.task_id,
            )
        object.__setattr__(self, "input", MappingProxyType(dict(payload)))

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "EvalTask":
        value = _require_mapping(payload, "task")
        _reject_unknown(value, _TASK_FIELDS, "task")
        missing = _TASK_FIELDS - set(value)
        if missing:
            raise EvalContractError(
                "eval_missing_field",
                f"评测任务缺少字段：{', '.join(sorted(missing))}",
                task_id=value.get("task_id") if isinstance(value.get("task_id"), str) else None,
            )
        return cls(
            schema_version=value["schema_version"],
            task_id=value["task_id"],
            category=value["category"],
            title=value["title"],
            runner=value["runner"],
            input=dict(_require_mapping(value["input"], "input")),
            expected=ExpectedResult.from_dict(value["expected"]),
            tags=tuple(_require_sequence(value["tags"], "tags")),
            expected_outcome=value["expected_outcome"],
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "task_id": self.task_id,
            "category": self.category,
            "title": self.title,
            "runner": self.runner,
            "input": dict(self.input),
            "expected": self.expected.to_dict(),
            "tags": list(self.tags),
            "expected_outcome": self.expected_outcome,
        }

    def public_input(self) -> dict[str, Any]:
        """Return the only task payload that may be shown to the system under test.

        Golden assertions and ``expected_outcome`` are intentionally absent.  The
        benchmark harness may use those fields, but an agent or runner must receive
        only this allowlisted projection.
        """

        return {
            "schema_version": self.schema_version,
            "task_id": self.task_id,
            "category": self.category,
            "title": self.title,
            "runner": self.runner,
            "input": dict(self.input),
            "tags": list(self.tags),
        }

    # Readable alias for harnesses that call the projection a payload.
    public_payload = public_input


@dataclass(frozen=True)
class TaskResult:
    """Runner-neutral result. ``actual`` is the runner's JSON output."""

    task_id: str
    status: str
    actual: Mapping[str, Any] = field(default_factory=dict)
    error_codes: tuple[str, ...] = ()
    tool_call_count: int = 0
    tool_attempt_count: int = 0
    tool_error_codes: tuple[str, ...] = ()
    latency_ms: float = 0.0
    cost_usd: float | None = None
    model_call_count: int = 0
    priced_model_call_count: int = 0
    evidence_ids: tuple[str, ...] = ()
    approval_state: str | None = None
    attempt_count: int | None = None
    handler_invocations: int | None = None
    safety_violation: bool = False

    def __post_init__(self) -> None:
        _require_nonempty_string(self.task_id, "result.task_id")
        _require_nonempty_string(self.status, "result.status")
        actual = _require_mapping(self.actual, "result.actual")
        _validate_json_value(actual, "result.actual")
        _validate_strings(self.error_codes, "result.error_codes")
        _validate_strings(self.tool_error_codes, "result.tool_error_codes")
        _require_nonnegative_int(self.tool_call_count, "result.tool_call_count")
        _require_nonnegative_int(
            self.tool_attempt_count, "result.tool_attempt_count"
        )
        if len(self.tool_error_codes) > self.tool_attempt_count:
            raise EvalContractError(
                "eval_inconsistent_tool_metrics",
                "result.tool_error_codes 不能多于 tool_attempt_count。",
            )
        _require_nonnegative_finite(self.latency_ms, "result.latency_ms")
        _require_nonnegative_int(
            self.model_call_count, "result.model_call_count"
        )
        _require_nonnegative_int(
            self.priced_model_call_count, "result.priced_model_call_count"
        )
        if self.priced_model_call_count > self.model_call_count:
            raise EvalContractError(
                "eval_inconsistent_cost_metrics",
                "priced_model_call_count 不能大于 model_call_count。",
            )
        if self.cost_usd is not None:
            _require_nonnegative_finite(self.cost_usd, "result.cost_usd")
        if self.model_call_count == 0:
            if self.cost_usd not in (None, 0, 0.0):
                raise EvalContractError(
                    "eval_inconsistent_cost_metrics",
                    "没有模型调用时 cost_usd 必须为 0 或 null。",
                )
            object.__setattr__(self, "cost_usd", 0.0)
        elif self.priced_model_call_count == 0 and self.cost_usd not in (None,):
            raise EvalContractError(
                "eval_inconsistent_cost_metrics",
                "模型价格未知时 cost_usd 必须为 null，不能记为 0。",
            )
        elif self.priced_model_call_count > 0 and self.cost_usd is None:
            raise EvalContractError(
                "eval_inconsistent_cost_metrics",
                "已定价的模型调用必须提供 cost_usd。",
            )
        _validate_unique_strings(self.evidence_ids, "result.evidence_ids")
        if self.approval_state is not None:
            _require_nonempty_string(
                self.approval_state, "result.approval_state"
            )
        _require_optional_nonnegative_int(
            self.attempt_count, "result.attempt_count"
        )
        _require_optional_nonnegative_int(
            self.handler_invocations, "result.handler_invocations"
        )
        if not isinstance(self.safety_violation, bool):
            raise EvalContractError(
                "eval_invalid_type", "result.safety_violation 必须是布尔值。"
            )
        object.__setattr__(self, "actual", MappingProxyType(dict(actual)))

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "TaskResult":
        value = _require_mapping(payload, "task result")
        _reject_unknown(value, _RESULT_FIELDS, "task result")
        missing = {"task_id", "status", "actual"} - set(value)
        if missing:
            raise EvalContractError(
                "eval_missing_field",
                f"任务结果缺少字段：{', '.join(sorted(missing))}",
            )
        return cls(
            task_id=value["task_id"],
            status=value["status"],
            actual=dict(_require_mapping(value["actual"], "result.actual")),
            error_codes=tuple(
                _require_sequence(value.get("error_codes", []), "result.error_codes")
            ),
            tool_call_count=value.get("tool_call_count", 0),
            tool_attempt_count=value.get("tool_attempt_count", 0),
            tool_error_codes=tuple(
                _require_sequence(
                    value.get("tool_error_codes", []), "result.tool_error_codes"
                )
            ),
            latency_ms=value.get("latency_ms", 0.0),
            cost_usd=value.get("cost_usd"),
            model_call_count=value.get("model_call_count", 0),
            priced_model_call_count=value.get("priced_model_call_count", 0),
            evidence_ids=tuple(
                _require_sequence(value.get("evidence_ids", []), "result.evidence_ids")
            ),
            approval_state=value.get("approval_state"),
            attempt_count=value.get("attempt_count"),
            handler_invocations=value.get("handler_invocations"),
            safety_violation=value.get("safety_violation", False),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "status": self.status,
            "actual": dict(self.actual),
            "error_codes": list(self.error_codes),
            "tool_call_count": self.tool_call_count,
            "tool_attempt_count": self.tool_attempt_count,
            "tool_error_codes": list(self.tool_error_codes),
            "latency_ms": self.latency_ms,
            "cost_usd": self.cost_usd,
            "model_call_count": self.model_call_count,
            "priced_model_call_count": self.priced_model_call_count,
            "evidence_ids": list(self.evidence_ids),
            "approval_state": self.approval_state,
            "attempt_count": self.attempt_count,
            "handler_invocations": self.handler_invocations,
            "safety_violation": self.safety_violation,
        }


@dataclass(frozen=True)
class TaskScore:
    task_id: str
    passed: bool
    outcome_match: bool
    status_match: bool
    error_code_match: bool
    tool_error_match: bool
    exact_match: bool
    numeric_match: bool
    evidence_match: bool
    approval_state_match: bool
    attempt_count_match: bool
    handler_invocations_match: bool
    safety_match: bool
    evidence_citations_matched: int
    evidence_citations_required: int
    unexpected_tool_error_count: int
    gross_tool_error_count: int
    failures: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["failures"] = list(self.failures)
        return value


@dataclass(frozen=True)
class EvalReport:
    schema_version: str
    task_count: int
    passed_count: int
    failed_count: int
    success_rate: float
    unexpected_tool_error_count: int
    tool_call_count: int
    tool_attempt_count: int
    unexpected_tool_error_rate: float
    gross_tool_error_count: int
    gross_tool_error_rate: float
    safety_violation_count: int
    safety_violation_rate: float
    evidence_citations_matched: int
    evidence_citations_required: int
    evidence_citation_accuracy: float
    p50_latency_ms: float
    p95_latency_ms: float
    model_call_count: int
    priced_model_call_count: int
    cost_coverage: float
    cost_status: str
    known_cost_usd: float
    total_cost_usd: float | None
    mean_cost_usd: float | None
    category_success_rates: Mapping[str, float]
    task_scores: tuple[TaskScore, ...]

    def to_dict(self) -> dict[str, Any]:
        value = {
            "schema_version": self.schema_version,
            "task_count": self.task_count,
            "passed_count": self.passed_count,
            "failed_count": self.failed_count,
            "success_rate": self.success_rate,
            "unexpected_tool_error_count": self.unexpected_tool_error_count,
            "tool_call_count": self.tool_call_count,
            "tool_attempt_count": self.tool_attempt_count,
            "unexpected_tool_error_rate": self.unexpected_tool_error_rate,
            "gross_tool_error_count": self.gross_tool_error_count,
            "gross_tool_error_rate": self.gross_tool_error_rate,
            "safety_violation_count": self.safety_violation_count,
            "safety_violation_rate": self.safety_violation_rate,
            "evidence_citations_matched": self.evidence_citations_matched,
            "evidence_citations_required": self.evidence_citations_required,
            "evidence_citation_accuracy": self.evidence_citation_accuracy,
            "p50_latency_ms": self.p50_latency_ms,
            "p95_latency_ms": self.p95_latency_ms,
            "model_call_count": self.model_call_count,
            "priced_model_call_count": self.priced_model_call_count,
            "cost_coverage": self.cost_coverage,
            "cost_status": self.cost_status,
            "known_cost_usd": self.known_cost_usd,
            "total_cost_usd": self.total_cost_usd,
            "mean_cost_usd": self.mean_cost_usd,
            "category_success_rates": dict(self.category_success_rates),
            "task_scores": [score.to_dict() for score in self.task_scores],
        }
        _validate_json_value(value, "eval report")
        return value


def load_eval_tasks(
    path: str | Path,
    *,
    expected_count: int | None = EXPECTED_TASK_COUNT,
) -> list[EvalTask]:
    """Strictly load a JSONL corpus; default acceptance size is exactly 50."""

    source = Path(path)
    try:
        lines = source.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as error:
        raise EvalContractError(
            "eval_file_unreadable", f"无法读取评测集：{error}"
        ) from error

    tasks: list[EvalTask] = []
    seen: set[str] = set()
    for line_number, line in enumerate(lines, start=1):
        if not line.strip():
            raise EvalContractError(
                "eval_blank_line",
                f"JSONL 第 {line_number} 行为空行。",
                line_number=line_number,
            )
        try:
            payload = json.loads(
                line,
                parse_constant=lambda token: _reject_json_constant(token),
                object_pairs_hook=_object_without_duplicate_keys,
            )
        except (json.JSONDecodeError, EvalContractError) as error:
            if isinstance(error, EvalContractError):
                message = str(error)
                code = error.code
            else:
                message = error.msg
                code = "eval_invalid_json"
            raise EvalContractError(
                code,
                f"JSONL 第 {line_number} 行无效：{message}",
                line_number=line_number,
            ) from error
        try:
            task = EvalTask.from_dict(payload)
        except EvalContractError as error:
            raise EvalContractError(
                error.code,
                f"JSONL 第 {line_number} 行无效：{error}",
                line_number=line_number,
                task_id=error.task_id,
            ) from error
        if task.task_id in seen:
            raise EvalContractError(
                "eval_duplicate_task_id",
                f"重复的 task_id：{task.task_id}",
                line_number=line_number,
                task_id=task.task_id,
            )
        seen.add(task.task_id)
        tasks.append(task)

    if expected_count is not None:
        _require_nonnegative_int(expected_count, "expected_count")
        if len(tasks) != expected_count:
            raise EvalContractError(
                "eval_task_count_mismatch",
                f"评测集必须包含 {expected_count} 个任务，实际为 {len(tasks)}。",
            )
    return tasks


def _reject_json_constant(token: str) -> None:
    raise EvalContractError(
        "eval_non_finite_number", f"JSON 不允许非有限数值 {token}。"
    )


def _object_without_duplicate_keys(
    pairs: list[tuple[str, Any]],
) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise EvalContractError(
                "eval_duplicate_json_key", f"JSON 对象包含重复键 {key!r}。"
            )
        result[key] = value
    return result


def _reject_unknown(
    payload: Mapping[str, Any], allowed: frozenset[str], label: str
) -> None:
    unknown = sorted(set(payload) - allowed)
    if unknown:
        raise EvalContractError(
            "eval_unknown_field",
            f"{label} 包含未知字段：{', '.join(unknown)}",
        )


def _require_mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or not all(
        isinstance(key, str) for key in value
    ):
        raise EvalContractError("eval_invalid_type", f"{label} 必须是 JSON 对象。")
    return value


def _require_sequence(value: Any, label: str) -> Sequence[Any]:
    if not isinstance(value, Sequence) or isinstance(
        value, (str, bytes, bytearray)
    ):
        raise EvalContractError("eval_invalid_type", f"{label} 必须是 JSON 数组。")
    return value


def _require_nonempty_string(value: Any, label: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise EvalContractError(
            "eval_invalid_type", f"{label} 必须是非空字符串。"
        )


def _validate_strings(values: Sequence[Any], label: str) -> None:
    for value in values:
        _require_nonempty_string(value, label)


def _validate_unique_strings(values: Sequence[Any], label: str) -> None:
    _validate_strings(values, label)
    if len(set(values)) != len(values):
        raise EvalContractError(
            "eval_duplicate_value", f"{label} 不允许重复值。"
        )


def _require_nonnegative_int(value: Any, label: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise EvalContractError(
            "eval_invalid_number", f"{label} 必须是非负整数。"
        )


def _require_optional_nonnegative_int(value: Any, label: str) -> None:
    if value is not None:
        _require_nonnegative_int(value, label)


def _require_finite_number(value: Any, label: str) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise EvalContractError(
            "eval_invalid_number", f"{label} 必须是数值。"
        )
    if not math.isfinite(value):
        raise EvalContractError(
            "eval_non_finite_number", f"{label} 不允许 NaN 或 Infinity。"
        )


def _require_nonnegative_finite(value: Any, label: str) -> None:
    _require_finite_number(value, label)
    if value < 0:
        raise EvalContractError(
            "eval_invalid_number", f"{label} 必须是非负数值。"
        )


def _validate_json_value(value: Any, label: str, *, _depth: int = 0) -> None:
    if _depth > 32:
        raise EvalContractError(
            "eval_max_depth", f"{label} 超过最大 JSON 嵌套深度。"
        )
    if value is None or isinstance(value, (str, bool, int)):
        return
    if isinstance(value, float):
        _require_finite_number(value, label)
        return
    if isinstance(value, Mapping):
        if not all(isinstance(key, str) for key in value):
            raise EvalContractError(
                "eval_invalid_type", f"{label} 的 JSON 对象键必须是字符串。"
            )
        for key, item in value.items():
            _validate_json_value(item, f"{label}.{key}", _depth=_depth + 1)
        return
    if isinstance(value, Sequence) and not isinstance(
        value, (str, bytes, bytearray)
    ):
        for index, item in enumerate(value):
            _validate_json_value(item, f"{label}[{index}]", _depth=_depth + 1)
        return
    raise EvalContractError(
        "eval_invalid_type", f"{label} 包含非 JSON 类型 {type(value).__name__}。"
    )
