from __future__ import annotations

from copy import deepcopy

import pytest

from pilot_staging.postgres import (
    _event_binding_matches_record,
    _event_binding_matches_tombstone,
)
from pilot_staging.telemetry import (
    EXECUTION_TELEMETRY_DIGEST_V2,
    execution_telemetry_event_binding,
    execution_telemetry_sha256,
    summarize_provider_execution_telemetry,
    validate_provider_execution_telemetry,
)


def _record(
    *,
    error_code: str | None = "provider_output_incomplete",
    completion_failure_source: str | None = None,
) -> dict[str, object]:
    return {
        "started_at": "2026-08-23T00:00:00Z",
        "status": "failed",
        "provider_latency_ms": 1250,
        "outcome": "controlled_failure",
        "error_code": error_code,
        "model_call_count": 1,
        "model_requested_tool_call_count": 0,
        "backend_executed_tool_call_count": 0,
        "completion_failure_source": completion_failure_source,
    }


def test_v1_digest_is_frozen_and_does_not_bind_v2_source() -> None:
    legacy = _record()
    assert execution_telemetry_sha256(legacy) == (
        "6d3184b104d439a24a9c8e1f111beb116adaf53212cdb8d15979446b14e5639b"
    )
    with_source = _record(completion_failure_source="final_output_missing")
    assert execution_telemetry_sha256(with_source) == execution_telemetry_sha256(
        legacy
    )


def test_versioned_event_binding_accepts_legacy_and_mixed_retention_history() -> None:
    legacy_record = _record()
    legacy_terminal = {
        "attempt_id": "attempt-1",
        "error_code": "provider_output_incomplete",
        "execution_telemetry_sha256": execution_telemetry_sha256(legacy_record),
    }
    assert _event_binding_matches_record(legacy_terminal, legacy_record) is True

    unbound_source = _record(completion_failure_source="final_output_missing")
    assert _event_binding_matches_record(legacy_terminal, unbound_source) is False

    v2_terminal = {
        "attempt_id": "attempt-1",
        "error_code": "provider_output_incomplete",
        **execution_telemetry_event_binding(legacy_record),
    }
    v2_tombstone = {
        "attempt_id": "attempt-1",
        **execution_telemetry_event_binding(legacy_record),
        "stable_error_code": "provider_output_incomplete",
    }
    assert _event_binding_matches_record(v2_terminal, legacy_record) is True
    assert _event_binding_matches_tombstone(legacy_terminal, v2_tombstone) is True
    assert _event_binding_matches_tombstone(v2_terminal, v2_tombstone) is True

    legacy_tombstone = {
        "attempt_id": "attempt-1",
        "execution_telemetry_sha256": execution_telemetry_sha256(legacy_record),
        "stable_error_code": "provider_output_incomplete",
    }
    assert _event_binding_matches_tombstone(v2_terminal, legacy_tombstone) is False

    unknown_version = deepcopy(v2_terminal)
    unknown_version["execution_telemetry_digest_version"] = "future-version"
    assert _event_binding_matches_record(unknown_version, legacy_record) is False

    missing_digest = deepcopy(v2_terminal)
    del missing_digest["execution_telemetry_v2_sha256"]
    assert _event_binding_matches_record(missing_digest, legacy_record) is False

    missing_source = deepcopy(v2_terminal)
    del missing_source["completion_failure_source"]
    assert _event_binding_matches_record(missing_source, legacy_record) is False

    markerless_source = deepcopy(legacy_terminal)
    markerless_source["completion_failure_source"] = "final_output_missing"
    assert _event_binding_matches_record(markerless_source, unbound_source) is False


def test_completion_failure_source_summary_has_independent_legacy_coverage() -> None:
    records = [
        _record(completion_failure_source="final_output_missing"),
        _record(completion_failure_source="response_output_item_incomplete"),
        _record(),
        _record(
            error_code="provider_output_not_completed",
            completion_failure_source="response_not_completed",
        ),
        _record(
            error_code="output_limit_suspected",
            completion_failure_source="output_limit_suspected",
        ),
        _record(error_code="provider_timeout"),
        _record(error_code=None),
    ]
    summary = summarize_provider_execution_telemetry(records)
    assert summary["telemetry_coverage_status"] == "complete"
    assert summary["completion_failure_sources"] == {
        "semantics_version": "pilot-completion-failure-source-v2",
        "applicable_attempt_count": 5,
        "observed_attempt_count": 4,
        "unknown_attempt_count": 1,
        "coverage_rate": 0.8,
        "coverage_status": "partial",
        "counts": [
            {"completion_failure_source": "final_output_missing", "attempt_count": 1},
            {"completion_failure_source": "output_limit_suspected", "attempt_count": 1},
            {"completion_failure_source": "response_not_completed", "attempt_count": 1},
            {
                "completion_failure_source": "response_output_item_incomplete",
                "attempt_count": 1,
            },
        ],
    }
    validate_provider_execution_telemetry(summary)

    invalid = deepcopy(summary)
    invalid["completion_failure_sources"]["observed_attempt_count"] = 5
    with pytest.raises(ValueError, match="denominator mismatch"):
        validate_provider_execution_telemetry(invalid)


def test_completion_failure_source_summary_rejects_error_or_outcome_drift() -> None:
    with pytest.raises(ValueError, match="稳定 error_code/outcome 不一致"):
        summarize_provider_execution_telemetry(
            [
                _record(
                    error_code="provider_output_not_completed",
                    completion_failure_source="final_output_missing",
                )
            ]
        )
    contradictory = _record(completion_failure_source="final_output_missing")
    contradictory["outcome"] = "completed"
    with pytest.raises(ValueError, match="稳定 error_code/outcome 不一致"):
        summarize_provider_execution_telemetry([contradictory])


def test_v2_binding_carries_only_allowlisted_source_not_provider_content() -> None:
    record = _record(completion_failure_source="final_output_missing")
    binding = execution_telemetry_event_binding(record)
    assert binding["execution_telemetry_digest_version"] == (
        EXECUTION_TELEMETRY_DIGEST_V2
    )
    serialized = repr(binding)
    assert "SECRET_RAW_PROVIDER_BODY" not in serialized
    assert set(binding) == {
        "execution_telemetry_sha256",
        "execution_telemetry_digest_version",
        "execution_telemetry_v2_sha256",
        "completion_failure_source",
    }
