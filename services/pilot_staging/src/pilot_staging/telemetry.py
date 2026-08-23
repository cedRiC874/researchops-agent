from __future__ import annotations

from collections import Counter
import hashlib
import json
import math
from typing import Any, Mapping, Sequence


_TERMINAL_STATUSES = {"succeeded", "failed", "withheld", "excluded", "completed"}
_NON_FAILURE_REASONS = {None, "participant_skipped", "participant_withdrew"}
_SAFETY_REASON = "pilot_output_safety_filter"


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
