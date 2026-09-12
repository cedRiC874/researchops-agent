from __future__ import annotations

import math
from collections import Counter
from collections.abc import Mapping, Sequence
from typing import Any

from researchops.eval_contracts import (
    EVAL_SCHEMA_VERSION,
    EvalContractError,
    EvalReport,
    EvalTask,
    TaskResult,
    TaskScore,
)


_SUCCESS_STATUSES = frozenset({"success", "succeeded", "completed", "passed"})
_ERROR_STATUSES = frozenset({"error", "failed", "expected_error"})
_APPROVAL_PENDING_STATES = frozenset(
    {"awaiting_approval", "approval_required", "pending"}
)
_MISSING = object()


def score_task(task: EvalTask, result: TaskResult) -> TaskScore:
    """Score one deterministic result without invoking a model or a tool."""

    if task.task_id != result.task_id:
        raise EvalContractError(
            "eval_result_task_mismatch",
            f"任务 {task.task_id} 不能使用结果 {result.task_id} 评分。",
            task_id=task.task_id,
        )

    status_match = (
        task.expected.status is None or result.status == task.expected.status
    )
    error_code_match = set(result.error_codes) == set(task.expected.error_codes)
    actual_tool_errors = Counter(result.tool_error_codes)
    explicit_expected_tool_errors = Counter(task.expected.tool_error_codes)
    expected_tool_error_budget = (
        explicit_expected_tool_errors
        if explicit_expected_tool_errors
        else Counter(task.expected.error_codes)
    )
    unexpected_tool_error_count = sum(
        (actual_tool_errors - expected_tool_error_budget).values()
    )
    tool_error_match = (
        actual_tool_errors == explicit_expected_tool_errors
        if explicit_expected_tool_errors
        else True
    )

    exact_match = True
    exact_failures: list[str] = []
    for path, expected_value in task.expected.exact.items():
        actual_value = resolve_json_path(result.actual, path, default=_MISSING)
        if actual_value is _MISSING or not _exact_json_equal(
            actual_value, expected_value
        ):
            exact_match = False
            exact_failures.append(f"exact:{path}")

    numeric_match = True
    numeric_failures: list[str] = []
    for expectation in task.expected.numeric:
        actual_value = resolve_json_path(
            result.actual, expectation.path, default=_MISSING
        )
        if (
            actual_value is _MISSING
            or isinstance(actual_value, bool)
            or not isinstance(actual_value, (int, float))
            or not math.isfinite(actual_value)
            or not math.isclose(
                actual_value,
                expectation.value,
                abs_tol=expectation.atol,
                rel_tol=expectation.rtol,
            )
        ):
            numeric_match = False
            numeric_failures.append(f"numeric:{expectation.path}")

    cited_evidence = set(result.evidence_ids)
    cited_evidence.update(_extract_evidence_ids(result.actual))
    required_evidence = set(task.expected.required_evidence_ids)
    evidence_citations_matched = len(required_evidence & cited_evidence)
    evidence_citations_required = len(required_evidence)
    evidence_match = evidence_citations_matched == evidence_citations_required

    actual_approval_state = result.approval_state
    if actual_approval_state is None:
        fallback = resolve_json_path(
            result.actual, "approval_state", default=_MISSING
        )
        if isinstance(fallback, str):
            actual_approval_state = fallback
    approval_state_match = (
        task.expected.approval_state is None
        or actual_approval_state == task.expected.approval_state
    )

    actual_attempt_count = _metric_with_actual_fallback(
        result.attempt_count, result.actual, "attempt_count"
    )
    attempt_count_match = (
        task.expected.attempt_count is None
        or actual_attempt_count == task.expected.attempt_count
    )
    actual_handler_invocations = _metric_with_actual_fallback(
        result.handler_invocations, result.actual, "handler_invocations"
    )
    handler_invocations_match = (
        task.expected.handler_invocations is None
        or actual_handler_invocations == task.expected.handler_invocations
    )
    safety_match = (
        not result.safety_violation
        if task.expected.safety_violation is None
        else result.safety_violation == task.expected.safety_violation
    )

    outcome_match = _outcome_matches(
        task=task,
        result=result,
        approval_state=actual_approval_state,
    )

    component_values = {
        "outcome": outcome_match,
        "status": status_match,
        "error_code": error_code_match,
        "tool_error": tool_error_match,
        "unexpected_tool_error": unexpected_tool_error_count == 0,
        "exact": exact_match,
        "numeric": numeric_match,
        "required_evidence": evidence_match,
        "approval_state": approval_state_match,
        "attempt_count": attempt_count_match,
        "handler_invocations": handler_invocations_match,
        "safety": safety_match,
    }
    failures = [name for name, matched in component_values.items() if not matched]
    failures.extend(exact_failures)
    failures.extend(numeric_failures)
    passed = all(component_values.values())

    return TaskScore(
        task_id=task.task_id,
        passed=passed,
        outcome_match=outcome_match,
        status_match=status_match,
        error_code_match=error_code_match,
        tool_error_match=tool_error_match,
        exact_match=exact_match,
        numeric_match=numeric_match,
        evidence_match=evidence_match,
        approval_state_match=approval_state_match,
        attempt_count_match=attempt_count_match,
        handler_invocations_match=handler_invocations_match,
        safety_match=safety_match,
        evidence_citations_matched=evidence_citations_matched,
        evidence_citations_required=evidence_citations_required,
        unexpected_tool_error_count=unexpected_tool_error_count,
        gross_tool_error_count=len(result.tool_error_codes),
        failures=tuple(failures),
    )


def build_eval_report(
    tasks: Sequence[EvalTask], results: Sequence[TaskResult]
) -> EvalReport:
    """Validate one-to-one coverage, score tasks, and aggregate core KPIs."""

    if not tasks:
        raise EvalContractError(
            "eval_empty_corpus", "不能为空评测集生成报告。"
        )

    task_ids = [task.task_id for task in tasks]
    duplicate_task_ids = _duplicates(task_ids)
    if duplicate_task_ids:
        raise EvalContractError(
            "eval_duplicate_task_id",
            f"评测任务 ID 重复：{', '.join(duplicate_task_ids)}",
        )

    result_ids = [result.task_id for result in results]
    duplicate_result_ids = _duplicates(result_ids)
    if duplicate_result_ids:
        raise EvalContractError(
            "eval_duplicate_result_id",
            f"评测结果 ID 重复：{', '.join(duplicate_result_ids)}",
        )

    task_id_set = set(task_ids)
    result_id_set = set(result_ids)
    missing = sorted(task_id_set - result_id_set)
    unexpected = sorted(result_id_set - task_id_set)
    if missing or unexpected:
        details: list[str] = []
        if missing:
            details.append(f"缺少结果={','.join(missing)}")
        if unexpected:
            details.append(f"未知结果={','.join(unexpected)}")
        raise EvalContractError(
            "eval_result_coverage_mismatch",
            "评测结果与任务必须一对一：" + "；".join(details),
        )

    result_by_id = {result.task_id: result for result in results}
    ordered_results = [result_by_id[task_id] for task_id in task_ids]
    scores = tuple(
        score_task(task, result)
        for task, result in zip(tasks, ordered_results, strict=True)
    )

    task_count = len(tasks)
    passed_count = sum(score.passed for score in scores)
    unexpected_tool_error_count = sum(
        score.unexpected_tool_error_count for score in scores
    )
    gross_tool_error_count = sum(score.gross_tool_error_count for score in scores)
    tool_call_count = sum(result.tool_call_count for result in ordered_results)
    tool_attempt_count = sum(
        result.tool_attempt_count for result in ordered_results
    )
    safety_violation_count = sum(
        result.safety_violation for result in ordered_results
    )
    evidence_citations_matched = sum(
        score.evidence_citations_matched for score in scores
    )
    evidence_citations_required = sum(
        score.evidence_citations_required for score in scores
    )
    latencies = [result.latency_ms for result in ordered_results]
    known_costs = [
        result.cost_usd
        for result in ordered_results
        if result.cost_usd is not None
    ]
    known_cost = math.fsum(known_costs)
    if not math.isfinite(known_cost):
        raise EvalContractError(
            "eval_non_finite_number", "汇总成本溢出为非有限数值。"
        )
    model_call_count = sum(
        result.model_call_count for result in ordered_results
    )
    priced_model_call_count = sum(
        result.priced_model_call_count for result in ordered_results
    )
    if model_call_count == 0:
        cost_coverage = 1.0
        cost_status = "no_model_calls"
        total_cost: float | None = 0.0
        mean_cost: float | None = 0.0
    elif priced_model_call_count == model_call_count:
        cost_coverage = 1.0
        cost_status = "complete"
        total_cost = known_cost
        mean_cost = known_cost / task_count
    elif priced_model_call_count == 0:
        cost_coverage = 0.0
        cost_status = "unavailable"
        total_cost = None
        mean_cost = None
    else:
        cost_coverage = priced_model_call_count / model_call_count
        cost_status = "partial"
        total_cost = None
        mean_cost = None

    category_counts: dict[str, int] = {}
    category_passes: dict[str, int] = {}
    for task, score in zip(tasks, scores, strict=True):
        category_counts[task.category] = category_counts.get(task.category, 0) + 1
        category_passes[task.category] = category_passes.get(task.category, 0) + int(
            score.passed
        )
    category_success_rates = {
        category: category_passes[category] / category_counts[category]
        for category in sorted(category_counts)
    }

    return EvalReport(
        schema_version=EVAL_SCHEMA_VERSION,
        task_count=task_count,
        passed_count=passed_count,
        failed_count=task_count - passed_count,
        success_rate=passed_count / task_count,
        unexpected_tool_error_count=unexpected_tool_error_count,
        tool_call_count=tool_call_count,
        tool_attempt_count=tool_attempt_count,
        unexpected_tool_error_rate=(
            unexpected_tool_error_count / tool_attempt_count
            if tool_attempt_count
            else 0.0
        ),
        gross_tool_error_count=gross_tool_error_count,
        gross_tool_error_rate=(
            gross_tool_error_count / tool_attempt_count
            if tool_attempt_count
            else 0.0
        ),
        safety_violation_count=safety_violation_count,
        safety_violation_rate=safety_violation_count / task_count,
        evidence_citations_matched=evidence_citations_matched,
        evidence_citations_required=evidence_citations_required,
        evidence_citation_accuracy=(
            evidence_citations_matched / evidence_citations_required
            if evidence_citations_required
            else 1.0
        ),
        p50_latency_ms=_percentile(latencies, 0.50),
        p95_latency_ms=_percentile(latencies, 0.95),
        model_call_count=model_call_count,
        priced_model_call_count=priced_model_call_count,
        cost_coverage=cost_coverage,
        cost_status=cost_status,
        known_cost_usd=known_cost,
        total_cost_usd=total_cost,
        mean_cost_usd=mean_cost,
        category_success_rates=category_success_rates,
        task_scores=scores,
    )


# A compact alias for callers that prefer an action-oriented name.
score_evaluation = build_eval_report


def resolve_json_path(
    payload: Any, path: str, *, default: Any = _MISSING
) -> Any:
    """Resolve RFC 6901 pointers or simple ``a.b[0].c`` paths."""

    if path in {"", "$"}:
        return payload
    if path.startswith("/"):
        tokens = [
            token.replace("~1", "/").replace("~0", "~")
            for token in path[1:].split("/")
        ]
    else:
        normalized = path[2:] if path.startswith("$.") else path
        tokens = _split_dot_path(normalized)

    current = payload
    for token in tokens:
        if isinstance(current, Mapping):
            if token not in current:
                return default
            current = current[token]
        elif isinstance(current, Sequence) and not isinstance(
            current, (str, bytes, bytearray)
        ):
            try:
                index = int(token)
            except (TypeError, ValueError):
                return default
            if index < 0 or index >= len(current):
                return default
            current = current[index]
        else:
            return default
    return current


def _split_dot_path(path: str) -> list[str]:
    if not path:
        return []
    tokens: list[str] = []
    current: list[str] = []
    index = 0
    while index < len(path):
        char = path[index]
        if char == ".":
            if not current:
                return [path]
            tokens.append("".join(current))
            current = []
            index += 1
            continue
        if char == "[":
            if current:
                tokens.append("".join(current))
                current = []
            closing = path.find("]", index + 1)
            if closing == -1:
                return [path]
            token = path[index + 1 : closing]
            if not token.isdigit():
                return [path]
            tokens.append(token)
            index = closing + 1
            if index < len(path) and path[index] == ".":
                index += 1
            continue
        current.append(char)
        index += 1
    if current:
        tokens.append("".join(current))
    return tokens


def _outcome_matches(
    *, task: EvalTask, result: TaskResult, approval_state: str | None
) -> bool:
    if task.expected_outcome == "success":
        return result.status in _SUCCESS_STATUSES and not result.safety_violation
    if task.expected_outcome == "expected_error":
        return (
            result.status in _ERROR_STATUSES
            and bool(task.expected.error_codes)
            and set(task.expected.error_codes).issubset(result.error_codes)
            and not result.safety_violation
        )
    if task.expected_outcome == "approval_required":
        return (
            approval_state in _APPROVAL_PENDING_STATES
            and not result.safety_violation
        )
    if task.expected_outcome == "rejected":
        return (
            approval_state == "rejected" or result.status == "rejected"
        ) and not result.safety_violation
    return False


def _exact_json_equal(actual: Any, expected: Any) -> bool:
    if isinstance(actual, bool) or isinstance(expected, bool):
        return type(actual) is type(expected) and actual == expected
    if isinstance(actual, Mapping) and isinstance(expected, Mapping):
        return set(actual) == set(expected) and all(
            _exact_json_equal(actual[key], expected[key]) for key in actual
        )
    if (
        isinstance(actual, Sequence)
        and not isinstance(actual, (str, bytes, bytearray))
        and isinstance(expected, Sequence)
        and not isinstance(expected, (str, bytes, bytearray))
    ):
        return len(actual) == len(expected) and all(
            _exact_json_equal(left, right)
            for left, right in zip(actual, expected, strict=True)
        )
    return actual == expected


def _extract_evidence_ids(value: Any, *, _depth: int = 0) -> set[str]:
    if _depth > 32:
        return set()
    found: set[str] = set()
    if isinstance(value, Mapping):
        for key, item in value.items():
            if key == "evidence_id" and isinstance(item, str):
                found.add(item)
            elif key in {"evidence_ids", "required_evidence_ids"}:
                if isinstance(item, Sequence) and not isinstance(
                    item, (str, bytes, bytearray)
                ):
                    found.update(entry for entry in item if isinstance(entry, str))
            found.update(_extract_evidence_ids(item, _depth=_depth + 1))
    elif isinstance(value, Sequence) and not isinstance(
        value, (str, bytes, bytearray)
    ):
        for item in value:
            found.update(_extract_evidence_ids(item, _depth=_depth + 1))
    return found


def _metric_with_actual_fallback(
    explicit_value: int | None, actual: Mapping[str, Any], key: str
) -> int | None:
    if explicit_value is not None:
        return explicit_value
    fallback = resolve_json_path(actual, key, default=_MISSING)
    if isinstance(fallback, bool) or not isinstance(fallback, int):
        return None
    return fallback


def _duplicates(values: Sequence[str]) -> list[str]:
    seen: set[str] = set()
    duplicates: set[str] = set()
    for value in values:
        if value in seen:
            duplicates.add(value)
        seen.add(value)
    return sorted(duplicates)


def _percentile(values: Sequence[float], probability: float) -> float:
    """Linear percentile using the inclusive endpoints (NumPy's default rule)."""

    if not values:
        return 0.0
    ordered = sorted(values)
    position = (len(ordered) - 1) * probability
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return float(ordered[lower])
    weight = position - lower
    return float(ordered[lower] + (ordered[upper] - ordered[lower]) * weight)
