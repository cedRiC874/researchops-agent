from __future__ import annotations

from collections import Counter
import hashlib
import json
import math
from typing import Any, Mapping, Sequence

from .domain import COMPLETION_FAILURE_SOURCES


_TERMINAL_STATUSES = {"succeeded", "failed", "withheld", "excluded", "completed"}
_NON_FAILURE_REASONS = {None, "participant_skipped", "participant_withdrew"}
_SAFETY_REASON = "pilot_output_safety_filter"
EXECUTION_TELEMETRY_DIGEST_V2 = "pilot-execution-telemetry-v2"
COMPLETION_FAILURE_SOURCE_SEMANTICS_V2 = "pilot-completion-failure-source-v2"
_COMPLETION_FAILURE_ERROR_BY_SOURCE = {
    "final_output_missing": "provider_output_incomplete",
    "response_output_item_incomplete": "provider_output_incomplete",
    "response_not_completed": "provider_output_not_completed",
    "output_limit_suspected": "output_limit_suspected",
}
_COMPLETION_FAILURE_ERROR_CODES = frozenset(
    _COMPLETION_FAILURE_ERROR_BY_SOURCE.values()
)


def summarize_provider_execution_telemetry(
    records: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Aggregate pre-filtered attempt metadata without treating unknown as zero."""

    started = [record for record in records if record.get("started_at") is not None]
    started_count = len(started)

    def metric(field: str) -> dict[str, int | float | None]:
        observed = [
            int(record[field]) for record in started if record.get(field) is not None
        ]
        observed_count = len(observed)
        unknown_count = started_count - observed_count
        return {
            "observed_sum": sum(observed) if observed else None,
            "observed_attempt_count": observed_count,
            "unknown_attempt_count": unknown_count,
            "coverage_rate": observed_count / started_count if started_count else None,
        }

    reasons = Counter(
        str(record.get("error_code"))
        for record in started
        if record.get("error_code") not in _NON_FAILURE_REASONS
    )
    model_calls = metric("model_call_count")
    requested_calls = metric("model_requested_tool_call_count")
    backend_calls = metric("backend_executed_tool_call_count")
    completion_failures = [
        record
        for record in started
        if record.get("error_code") in _COMPLETION_FAILURE_ERROR_CODES
    ]
    completion_sources: Counter[str] = Counter()
    completion_unknown_count = 0
    for record in completion_failures:
        source = record.get("completion_failure_source")
        if source is None:
            completion_unknown_count += 1
            continue
        if (
            not isinstance(source, str)
            or source not in COMPLETION_FAILURE_SOURCES
            or _COMPLETION_FAILURE_ERROR_BY_SOURCE[source] != record.get("error_code")
            or record.get("outcome") != "controlled_failure"
        ):
            raise ValueError(
                "completion_failure_source 与稳定 error_code/outcome 不一致。"
            )
        completion_sources[source] += 1
    completion_applicable_count = len(completion_failures)
    completion_observed_count = sum(completion_sources.values())
    completion_coverage_status = (
        "no_applicable_failures"
        if completion_applicable_count == 0
        else "complete" if completion_unknown_count == 0 else "partial"
    )
    coverage_status = (
        "no_attempts"
        if started_count == 0
        else (
            "complete"
            if all(
                item["unknown_attempt_count"] == 0
                for item in (model_calls, requested_calls, backend_calls)
            )
            else "partial"
        )
    )
    summary = {
        "telemetry_counter_semantics_version": "pilot-provider-telemetry-v1",
        "scope": "consented_non_withdrawn_participants_only",
        "withdrawn_participant_attempts_included": False,
        "worker_started_attempt_count": started_count,
        "terminal_attempt_count": sum(
            str(record.get("status")) in _TERMINAL_STATUSES for record in started
        ),
        "technical_failure_attempt_count": sum(
            record.get("error_code") not in _NON_FAILURE_REASONS
            and record.get("error_code") != _SAFETY_REASON
            for record in started
        ),
        "safety_withheld_attempt_count": sum(
            record.get("error_code") == _SAFETY_REASON for record in started
        ),
        "participant_skipped_after_execution_count": sum(
            record.get("error_code") == "participant_skipped" for record in started
        ),
        "failure_reason_counts": [
            {"reason_code": reason, "attempt_count": reasons[reason]}
            for reason in sorted(reasons)
        ],
        "completion_failure_sources": {
            "semantics_version": COMPLETION_FAILURE_SOURCE_SEMANTICS_V2,
            "applicable_attempt_count": completion_applicable_count,
            "observed_attempt_count": completion_observed_count,
            "unknown_attempt_count": completion_unknown_count,
            "coverage_rate": (
                completion_observed_count / completion_applicable_count
                if completion_applicable_count
                else None
            ),
            "coverage_status": completion_coverage_status,
            "counts": [
                {
                    "completion_failure_source": source,
                    "attempt_count": completion_sources[source],
                }
                for source in sorted(completion_sources)
            ],
        },
        "executor_model_call_count": model_calls,
        "model_requested_tool_call_count": requested_calls,
        "backend_executed_tool_call_count": backend_calls,
        "telemetry_coverage_status": coverage_status,
        "token_usage_collection_status": "not_collected",
        "cost_coverage_status": "unavailable",
        "upstream_api_request_total_claim_allowed": False,
        "campaign_wide_billing_total_claim_allowed": False,
        "model_planning_accuracy_claim_allowed": False,
        "append_only_event_binding_status": "not_applicable",
    }
    validate_provider_execution_telemetry(summary)
    return summary


def validate_provider_execution_telemetry(value: Mapping[str, Any]) -> None:
    started_count = int(value["worker_started_attempt_count"])
    metrics = (
        value["executor_model_call_count"],
        value["model_requested_tool_call_count"],
        value["backend_executed_tool_call_count"],
    )
    for metric in metrics:
        observed = int(metric["observed_attempt_count"])
        unknown = int(metric["unknown_attempt_count"])
        observed_sum = metric["observed_sum"]
        coverage = metric["coverage_rate"]
        if observed + unknown != started_count:
            raise ValueError("telemetry observed/unknown denominator mismatch。")
        if (observed == 0) != (observed_sum is None):
            raise ValueError("telemetry observed_sum nullability mismatch。")
        expected_coverage = observed / started_count if started_count else None
        if expected_coverage is None:
            if coverage is not None:
                raise ValueError("telemetry no-attempt coverage 必须为 null。")
        elif coverage is None or not math.isclose(
            float(coverage), expected_coverage, rel_tol=0.0, abs_tol=1e-12
        ):
            raise ValueError("telemetry coverage_rate mismatch。")
    expected_status = (
        "no_attempts"
        if started_count == 0
        else (
            "complete"
            if all(int(metric["unknown_attempt_count"]) == 0 for metric in metrics)
            else "partial"
        )
    )
    if value["telemetry_coverage_status"] != expected_status:
        raise ValueError("telemetry_coverage_status mismatch。")
    if int(value["terminal_attempt_count"]) > started_count:
        raise ValueError("terminal attempt count 超过 worker-started denominator。")
    if value["append_only_event_binding_status"] not in {
        "valid",
        "invalid",
        "not_applicable",
    }:
        raise ValueError("append_only_event_binding_status 无效。")
    completion = value["completion_failure_sources"]
    if completion["semantics_version"] != COMPLETION_FAILURE_SOURCE_SEMANTICS_V2:
        raise ValueError("completion failure source semantics version 无效。")
    applicable = int(completion["applicable_attempt_count"])
    observed = int(completion["observed_attempt_count"])
    unknown = int(completion["unknown_attempt_count"])
    if applicable > started_count or observed + unknown != applicable:
        raise ValueError("completion failure source denominator mismatch。")
    raw_counts = completion["counts"]
    sources: list[str] = []
    counted = 0
    for item in raw_counts:
        source = item["completion_failure_source"]
        attempt_count = int(item["attempt_count"])
        if source not in COMPLETION_FAILURE_SOURCES or attempt_count < 1:
            raise ValueError("completion failure source count 无效。")
        sources.append(source)
        counted += attempt_count
    if sources != sorted(set(sources)) or counted != observed:
        raise ValueError("completion failure source counts mismatch。")
    expected_completion_coverage = observed / applicable if applicable else None
    completion_coverage = completion["coverage_rate"]
    if expected_completion_coverage is None:
        if completion_coverage is not None:
            raise ValueError("completion failure source 空分母 coverage 必须为 null。")
    elif completion_coverage is None or not math.isclose(
        float(completion_coverage),
        expected_completion_coverage,
        rel_tol=0.0,
        abs_tol=1e-12,
    ):
        raise ValueError("completion failure source coverage mismatch。")
    expected_completion_status = (
        "no_applicable_failures"
        if applicable == 0
        else "complete" if unknown == 0 else "partial"
    )
    if completion["coverage_status"] != expected_completion_status:
        raise ValueError("completion failure source coverage status mismatch。")


def execution_telemetry_sha256(record: Mapping[str, Any]) -> str:
    canonical = json.dumps(
        {
            "provider_latency_ms": record.get("provider_latency_ms"),
            "outcome": record.get("outcome"),
            "model_call_count": record.get("model_call_count"),
            "model_requested_tool_call_count": record.get(
                "model_requested_tool_call_count"
            ),
            "backend_executed_tool_call_count": record.get(
                "backend_executed_tool_call_count"
            ),
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def execution_telemetry_v2_sha256(record: Mapping[str, Any]) -> str:
    canonical = json.dumps(
        {
            "digest_version": EXECUTION_TELEMETRY_DIGEST_V2,
            "provider_latency_ms": record.get("provider_latency_ms"),
            "outcome": record.get("outcome"),
            "model_call_count": record.get("model_call_count"),
            "model_requested_tool_call_count": record.get(
                "model_requested_tool_call_count"
            ),
            "backend_executed_tool_call_count": record.get(
                "backend_executed_tool_call_count"
            ),
            "completion_failure_source": record.get("completion_failure_source"),
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def execution_telemetry_event_binding(record: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "execution_telemetry_sha256": execution_telemetry_sha256(record),
        "execution_telemetry_digest_version": EXECUTION_TELEMETRY_DIGEST_V2,
        "execution_telemetry_v2_sha256": execution_telemetry_v2_sha256(record),
        "completion_failure_source": record.get("completion_failure_source"),
    }
