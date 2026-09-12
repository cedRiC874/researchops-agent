"""Pure semantic verification for persisted T6-C closure artifacts.

The caller supplies already-decoded JSON and rows already extracted from the
container-verified, read-only SQLite database.  This module intentionally has
no file, database, environment, clock, key, Provider, or network capability.
It defensively snapshots every caller-owned JSON value before validating it and
returns only an opaque, content-free summary.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal, Inexact, InvalidOperation, localcontext
from typing import Any, NoReturn

from researchops_completion_telemetry.persisted_evidence import (
    summarize_persisted_live_runtime_denominator_artifact,
    validate_persisted_live_completion_record,
)

from .errors import ExternalClosurePrimitiveError
from .primitives import (
    canonical_json_bytes,
    decode_strict_json_object,
    parse_utc_timestamp,
)


_MAX_SAFE_INTEGER = 9_007_199_254_740_991
_MAX_RUNS = 100
_MAX_ATTEMPTS = 800
_MAX_EVENTS = 20_000
_MAX_EVENT_PAYLOAD_BYTES = 2_000_000

_AUDIT_EVENT_DOMAIN = b"researchops.audit.v1"
_AUDIT_RUN_ID_DOMAIN = "researchops-provider-completion-audit-run-id-v1"
_AUDIT_RUN_SET_DOMAIN = "researchops-provider-completion-audit-run-set-v1"
_EXECUTION_INPUT_ORDER_DOMAIN = (
    "researchops-provider-completion-execution-input-order-v1"
)
_RUNTIME_PLAN_DOMAIN = "researchops-provider-completion-external-runtime-plan-v1"

_ZERO_HASH = "0" * 64
_SHA256 = re.compile(r"^[0-9a-f]{64}$", re.ASCII)
_NONZERO_SHA256 = re.compile(r"^(?!0{64}$)[0-9a-f]{64}$", re.ASCII)
_CASE_HANDLE = re.compile(r"^PCECASE-[A-F0-9]{32}$", re.ASCII)
_RUN_ID = re.compile(r"^PCERUN-[A-F0-9]{32}$", re.ASCII)
_ENVELOPE_ID = re.compile(r"^PCEPR-[A-F0-9]{32}$", re.ASCII)
_CAMPAIGN_ID = re.compile(r"^PCECAMP-[A-F0-9]{32}$", re.ASCII)
_MODEL_CALL_ID = re.compile(r"^MODEL-[A-F0-9]{16}$", re.ASCII)
_SAFE_ERROR = re.compile(r"^[a-z0-9_]{1,96}$", re.ASCII)
_SAFE_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$", re.ASCII)
_MONEY = re.compile(r"^(?!0\.000000$)(0|[1-9][0-9]{0,6})\.[0-9]{6}$", re.ASCII)

_CASE_RUN_TERMINAL_ERROR_CODES = frozenset(
    {
        "pce_transport_failed",
        "pce_transport_timed_out",
        "pce_transport_cancelled",
        "pce_transport_outcome_unknown",
        "pce_response_failed",
        "pce_response_timed_out",
        "pce_response_cancelled",
        "pce_response_outcome_unknown",
        "pce_raw_response_cleanup_failed",
        "pce_raw_response_cleanup_timed_out",
        "pce_raw_response_cleanup_cancelled",
    }
)

_OUTER_TELEMETRY_FIELDS = frozenset(
    {
        "schema_version",
        "status",
        "runtime_plan",
        "runtime_denominator",
        "ledger_reconciliation",
        "closure",
        "historical_backfill_performed",
    }
)
_EXTERNAL_RUNTIME_PLAN_FIELDS = frozenset(
    {
        "schema_version",
        "external_plan_binding_sha256",
        "denominator_plan_commitment_sha256",
        "denominator_plan",
        "telemetry_interpretation",
        "campaign_topology",
        "transport_limits",
        "budget_policy",
    }
)
_TELEMETRY_INTERPRETATION_FIELDS = frozenset(
    {"output_counter_comparability", "output_counter_path"}
)
_CAMPAIGN_TOPOLOGY_FIELDS = frozenset(
    {
        "campaign_id",
        "case_handle_set_commitment_sha256",
        "case_handle_order_commitment_sha256",
        "audit_run_id_derivation",
        "audit_run_set_commitment_sha256",
        "one_audit_run_per_case_handle",
    }
)
_TRANSPORT_LIMIT_FIELDS = frozenset(
    {
        "network_attempt_cap",
        "concurrency",
        "resume",
        "fallback",
        "tools",
        "request_timeout_seconds",
        "total_timeout_seconds",
    }
)
_BUDGET_FIELDS = frozenset(
    {
        "budget_policy_commitment_sha256",
        "pricing_snapshot_date",
        "pricing_source_url",
        "pricing_source_snapshot_sha256",
        "pricing_retrieved_at_utc",
        "official_pricing_attestation_current",
        "currency",
        "token_price_unit",
        "input_price_per_million_cny",
        "output_price_per_million_cny",
        "cache_discount_assumed",
        "input_token_limit_total",
        "output_token_limit_per_request",
        "output_token_limit_total",
        "local_observed_cost_stop_cny",
        "request_count_and_output_caps_enforced_pre_send",
        "input_token_limit_enforcement",
        "cost_stop_enforcement",
        "single_in_flight_request_may_overshoot_observed_limits",
        "provider_invoice_hard_cap",
        "provider_side_retention_verified",
    }
)
_DENOMINATOR_PLAN_BINDING_FIELDS = (
    "provider_id",
    "api_surface",
    "transport_id",
    "adapter_version",
    "telemetry_schema_sha256",
    "mapping_schema_version",
    "mapping_version",
    "mapping_sha256",
)

_INDEX_FIELDS = frozenset({"schema_version", "runs"})
_INDEX_RUN_FIELDS = frozenset(
    {
        "task_id",
        "run_id",
        "provider",
        "transport",
        "chain_verification",
        "completion_telemetry_event_commitment",
    }
)
_CHAIN_FIELDS = frozenset(
    {
        "valid",
        "run_id",
        "event_count",
        "chain_head",
        "error_code",
        "error_sequence",
    }
)
_BRIDGE_FIELDS = frozenset(
    {
        "schema_version",
        "case_id",
        "binding_sha256",
        "started",
        "terminals",
        "all_started_attempts_terminal",
        "write_failed",
        "commitment_sha256",
    }
)
_BRIDGE_HASH_FIELDS = frozenset({"attempt_index", "event_hash"})

_RUN_ROW_FIELDS = frozenset(
    {
        "run_id",
        "mode",
        "status",
        "request_sha256",
        "dataset_sha256",
        "created_at_utc",
        "updated_at_utc",
        "terminal_error_code",
    }
)
_EVENT_ROW_FIELDS = frozenset(
    {
        "event_id",
        "run_id",
        "sequence",
        "event_type",
        "occurred_at_utc",
        "actor_kind",
        "safe_payload_json",
        "prev_hash",
        "event_hash",
    }
)
_MODEL_CALL_ROW_FIELDS = frozenset(
    {
        "model_call_id",
        "run_id",
        "provider",
        "model",
        "started_at_utc",
        "latency_ms",
        "input_tokens",
        "output_tokens",
        "cached_tokens",
        "cost_usd",
        "outcome",
        "error_code",
    }
)

_START_FIELDS = frozenset(
    {"schema_version", "case_id", "attempt_index", "case_attempt_index", "binding"}
)
_TERMINAL_FIELDS = frozenset(
    {
        "schema_version",
        "case_id",
        "attempt_index",
        "case_attempt_index",
        "terminal_kind",
        "response_index",
        "error_code",
        "binding",
    }
)
_SEND_FIELDS = frozenset(
    {
        "schema_version",
        "case_id",
        "attempt_index",
        "case_attempt_index",
        "network_call_index",
        "provider_id",
        "api_surface",
        "transport_id",
        "adapter_version",
        "method",
        "origin",
        "path",
        "requested_output_token_cap",
        "execution_input_commitment_sha256",
        "audit_request_sha256",
        "external_plan_binding_sha256",
        "authorization_grant_sha256",
        "headers_persisted",
        "request_body_persisted",
        "task_content_persisted",
    }
)
_RUN_STARTED_FIELDS = frozenset({"mode", "request_sha256", "dataset_sha256"})
_MODEL_CALL_PAYLOAD_FIELDS = frozenset(
    {
        "model_call_id",
        "provider",
        "model",
        "latency_ms",
        "input_tokens",
        "output_tokens",
        "cached_tokens",
        "cost_usd",
        "outcome",
        "error_code",
    }
)
_MODEL_USAGE_FIELDS = frozenset(
    {
        "usage_complete",
        "request_count",
        "input_unit_count",
        "output_unit_count",
        "total_unit_count",
        "cached_input_unit_count",
        "response_detail_count",
        "estimated_cost_usd",
        "model_call_rows_recorded",
        "latency_allocation",
        "model_call_cost_method",
        "provider",
        "transport",
    }
)
_RUN_STATUS_FIELDS = frozenset({"from", "to", "error_code"})

_TERMINAL_EVENT_BY_KIND = {
    "response_accepted": "model_response_telemetry_recorded",
    "response_rejected": "model_response_telemetry_rejected",
    "http_error": "model_request_http_error",
    "no_response": "model_request_no_response",
    "cancelled": "model_request_cancelled",
    "outcome_unknown": "model_request_outcome_unknown",
}
_TERMINAL_EVENT_TYPES = frozenset(
    {
        "model_response_telemetry_recorded",
        "provider_completion_mapping_unmapped",
        "model_response_telemetry_rejected",
        "model_request_http_error",
        "model_request_no_response",
        "model_request_cancelled",
        "model_request_outcome_unknown",
    }
)
_ALLOWED_EVENT_TYPES = frozenset(
    {
        "run_started",
        "model_request_started",
        "provider_transport_request_sent",
        *_TERMINAL_EVENT_TYPES,
        "model_call_recorded",
        "model_run_usage_recorded",
        "run_status_changed",
    }
)
_SEND_REQUIRED_EVENTS = frozenset(
    {
        "model_response_telemetry_recorded",
        "provider_completion_mapping_unmapped",
        "model_response_telemetry_rejected",
        "model_request_http_error",
        "model_request_outcome_unknown",
    }
)
_NO_SEND_EVENTS = frozenset(
    {"model_request_no_response", "model_request_cancelled"}
)
_TERMINAL_ERROR_CODES = {
    "model_response_telemetry_recorded": frozenset({None}),
    "provider_completion_mapping_unmapped": frozenset({None}),
    "model_response_telemetry_rejected": frozenset(
        {"provider_completion_capture_failed", "provider_completion_raw_cleanup_failed"}
    ),
    "model_request_http_error": frozenset({"provider_completion_http_error"}),
    "model_request_no_response": frozenset({"provider_completion_no_response"}),
    "model_request_cancelled": frozenset({None}),
    "model_request_outcome_unknown": frozenset(
        {"provider_completion_outcome_unknown"}
    ),
}

_LEDGER_RECONCILIATION_FIELDS = frozenset(
    {
        "all_chains_valid",
        "ledger_export_failed",
        "ledger_failure_observed",
        "event_counts",
        "reasons",
    }
)
_INNER_CLOSURE_FIELDS = frozenset(
    {
        "claim_allowed",
        "claim_scope",
        "denominator_algorithm",
        "exact_response_count_preregistered",
        "derived_after_run",
        "observed_response_count",
        "reasons",
    }
)
_CLOSURE_REASON_ORDER = (
    "planned_cases_not_finalized",
    "no_model_attempts_observed",
    "no_provider_responses_observed",
    "local_terminal_outcome_not_succeeded",
    "task_not_released",
    "provider_key_not_loaded",
    "task_bundle_opening_unverified",
    "leak_canary_injection_unverified",
    "raw_response_cleanup_incomplete",
    "post_request_cleanup_incomplete",
    "provider_key_reference_not_released",
    "planned_case_without_accepted_response",
    "response_telemetry_rejected",
    "http_error_response_observed",
    "request_failed_without_response",
    "model_request_cancelled",
    "model_request_outcome_unknown",
    "sdk_raw_response_count_mismatched",
    "sdk_raw_response_count_unavailable",
    "sdk_usage_request_count_mismatched",
    "sdk_usage_request_count_unavailable",
    "completion_state_projection_mismatch",
    "completion_record_denominator_mismatch",
    "completion_state_unmapped",
    "completion_state_not_provided",
    "completion_state_not_persisted",
    "completion_state_unrecognized",
    "truncation_signal_token_cap_fallback",
    "truncation_signal_none",
    "truncation_signal_unrecognized",
    "audit_chain_or_export_invalid",
    "completion_telemetry_ledger_write_failed",
    "model_request_started_count_mismatch",
    "model_request_terminal_count_mismatch",
    "transport_send_count_mismatch",
    "transport_retry_or_cap_violation",
    "accepted_response_event_count_mismatch",
    "rejected_response_event_count_mismatch",
    "legacy_response_usage_event_present",
    "ledger_event_commitment_missing",
    "ledger_event_commitment_mismatch",
    "ledger_event_payload_mismatch",
    "record_usage_incomplete",
    "record_usage_index_coverage_mismatch",
    "observed_input_token_cap_exceeded",
    "observed_output_token_cap_exceeded",
    "observed_cost_stop_exceeded",
)

_SUMMARY_TOKEN = object()


@dataclass(frozen=True, slots=True, init=False)
class VerifiedArtifactContentSemantics:
    """Frozen, ordinal/count/hash-only result of full content verification."""

    observed_run_count: int
    full_plan_observed: bool
    observed_audit_run_set_commitment_sha256: str
    ordered_final_chain_heads_commitment_sha256: str
    planned_case_count: int
    attempt_count: int
    accepted_response_count: int
    network_attempt_count_observed: int
    model_request_count_observed: int
    model_call_count: int
    usage_complete: bool
    observed_input_tokens: int | None
    observed_output_tokens: int | None
    observed_cost_cny: str | None
    all_chains_valid: bool
    ledger_failure_observed: bool
    inner_closure_claim_allowed: bool
    inner_closure_reasons: tuple[str, ...]
    ledger_reconciliation_reasons: tuple[str, ...]
    telemetry_closure_claim_allowed: bool
    telemetry_closure_reasons: tuple[str, ...]
    semantic_closure_claim_allowed: bool
    semantic_closure_reasons: tuple[str, ...]
    last_observed_run_terminal_error_code: str | None

    def __init__(self, *args: object, **kwargs: object) -> None:
        del args, kwargs
        raise TypeError(
            "VerifiedArtifactContentSemantics requires semantic verification"
        )

    @classmethod
    def _create(
        cls, token: object, **values: object
    ) -> "VerifiedArtifactContentSemantics":
        if token is not _SUMMARY_TOKEN or set(values) != set(cls.__dataclass_fields__):
            raise TypeError("verified_artifact_semantics_construction_forbidden")
        instance = object.__new__(cls)
        for name in cls.__dataclass_fields__:
            object.__setattr__(instance, name, values[name])
        return instance


@dataclass(frozen=True, slots=True)
class _Plan:
    raw: dict[str, Any]
    denominator: dict[str, Any]
    binding: dict[str, Any]
    case_ids: tuple[str, ...]
    run_ids: tuple[str, ...]
    provider_id: str
    api_surface: str
    transport_id: str
    adapter_version: str
    external_plan_binding_sha256: str
    network_attempt_cap: int
    max_turns_per_case: int
    total_model_request_cap: int
    output_token_limit_per_request: int
    input_token_limit_total: int
    output_token_limit_total: int
    input_price: Decimal
    output_price: Decimal
    cost_stop: Decimal


@dataclass(frozen=True, slots=True)
class _Event:
    row: dict[str, Any]
    payload: dict[str, Any]


@dataclass(frozen=True, slots=True)
class _AttemptEvents:
    started: _Event
    sent: _Event | None
    terminal: _Event


@dataclass(frozen=True, slots=True)
class _RunEvents:
    run: dict[str, Any]
    events: tuple[_Event, ...]
    attempts: tuple[_AttemptEvents, ...]
    model_call_events: tuple[_Event, ...]
    usage_event: _Event
    status_event: _Event


@dataclass(frozen=True, slots=True)
class _EventAnalysis:
    runs: tuple[_RunEvents, ...]
    event_counts: dict[str, int]
    network_attempt_count: int
    model_request_count: int
    model_call_count: int
    ledger_failure_observed: bool
    transport_retry_or_cap_violation: bool
    record_usage_index_coverage_complete: bool
    execution_input_commitments: tuple[str, ...]


def _fail(code: str) -> NoReturn:
    raise ExternalClosurePrimitiveError(code)


def _exact(value: object, fields: frozenset[str], code: str) -> dict[str, Any]:
    if type(value) is not dict or set(value) != fields:
        _fail(code)
    return value


def _is_sha256(value: object, *, nonzero: bool = True) -> bool:
    pattern = _NONZERO_SHA256 if nonzero else _SHA256
    return type(value) is str and pattern.fullmatch(value) is not None


def _safe_int(value: object, *, maximum: int = _MAX_SAFE_INTEGER) -> bool:
    return type(value) is int and 0 <= value <= maximum


def _snapshot_mapping(value: Mapping[str, Any], code: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        _fail(code)
    try:
        payload = canonical_json_bytes(dict(value))
        return decode_strict_json_object(payload, max_bytes=100_000_000)
    except ExternalClosurePrimitiveError:
        _fail(code)
    except Exception:
        _fail(code)


def _snapshot_rows(
    rows: Sequence[Mapping[str, Any]], *, maximum: int, code: str
) -> tuple[dict[str, Any], ...]:
    if not isinstance(rows, Sequence) or isinstance(rows, (str, bytes, bytearray)):
        _fail(code)
    if len(rows) > maximum:
        _fail(code)
    try:
        payload = canonical_json_bytes([dict(row) for row in rows])
        decoded = json.loads(payload.decode("utf-8"))
    except Exception:
        _fail(code)
    if type(decoded) is not list or any(type(row) is not dict for row in decoded):
        _fail(code)
    return tuple(decoded)


def _hash_canonical(value: object) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def _json_equal(left: object, right: object) -> bool:
    """Compare JSON projections without Python's ``False == 0`` coercion."""

    return canonical_json_bytes(left) == canonical_json_bytes(right)


def _domain_hash(domain: str, *parts: bytes) -> str:
    return hashlib.sha256(
        domain.encode("utf-8") + b"\x00" + b"\x00".join(parts)
    ).hexdigest()


def _derive_run_id(envelope_id: str, case_id: str) -> str:
    digest = hashlib.sha256(
        _AUDIT_RUN_ID_DOMAIN.encode("utf-8")
        + b"\x00"
        + envelope_id.encode("ascii")
        + b"\x00"
        + case_id.encode("ascii")
    ).hexdigest()
    return "PCERUN-" + digest[:32].upper()


def _ordered_reasons(*groups: Sequence[str]) -> tuple[str, ...]:
    present = {reason for group in groups for reason in group}
    unknown = present.difference(_CLOSURE_REASON_ORDER)
    if unknown:
        _fail("closure_outer_projection_invalid")
    return tuple(reason for reason in _CLOSURE_REASON_ORDER if reason in present)


def _decimal_money(value: object) -> Decimal:
    if type(value) is not str or _MONEY.fullmatch(value) is None:
        _fail("closure_budget_invalid")
    try:
        result = Decimal(value)
    except InvalidOperation:
        _fail("closure_budget_invalid")
    if not result.is_finite() or result <= 0:
        _fail("closure_budget_invalid")
    return result


def _observed_money(value: Decimal) -> str:
    if not value.is_finite() or value < 0:
        _fail("closure_budget_invalid")
    quantum = Decimal("0.000000000001")
    try:
        with localcontext() as context:
            context.prec = 80
            context.traps[Inexact] = True
            quantized = value.quantize(quantum)
    except (InvalidOperation, Inexact):
        _fail("closure_budget_invalid")
    rendered = format(quantized, "f")
    integer, dot, fraction = rendered.partition(".")
    if dot != "." or len(fraction) != 12 or len(integer) > 18:
        _fail("closure_budget_invalid")
    return rendered


def _exact_cost(
    input_tokens: int,
    output_tokens: int,
    input_price: Decimal,
    output_price: Decimal,
) -> Decimal:
    """Compute the CNY cost with enough precision and no implicit rounding."""

    try:
        with localcontext() as context:
            context.prec = 80
            context.traps[Inexact] = True
            return (
                Decimal(input_tokens) * input_price
                + Decimal(output_tokens) * output_price
            ) / Decimal(1_000_000)
    except (InvalidOperation, Inexact):
        _fail("closure_budget_invalid")


def _validate_plan(
    expected_runtime_plan: Mapping[str, Any],
    *,
    envelope_id: str,
    campaign_id: str,
) -> _Plan:
    plan = _snapshot_mapping(expected_runtime_plan, "closure_denominator_invalid")
    _exact(plan, _EXTERNAL_RUNTIME_PLAN_FIELDS, "closure_denominator_invalid")
    if (
        type(envelope_id) is not str
        or _ENVELOPE_ID.fullmatch(envelope_id) is None
        or type(campaign_id) is not str
        or _CAMPAIGN_ID.fullmatch(campaign_id) is None
        or plan["schema_version"]
        != "provider-completion-external-runtime-plan/1.0"
    ):
        _fail("closure_denominator_invalid")

    denominator = plan.get("denominator_plan")
    if type(denominator) is not dict:
        _fail("closure_denominator_invalid")
    case_values = denominator.get("case_ids")
    if (
        type(case_values) is not list
        or not 1 <= len(case_values) <= _MAX_RUNS
        or any(
            type(case_id) is not str or _CASE_HANDLE.fullmatch(case_id) is None
            for case_id in case_values
        )
        or len(case_values) != len(set(case_values))
    ):
        _fail("closure_denominator_invalid")
    case_ids = tuple(case_values)
    run_ids = tuple(_derive_run_id(envelope_id, case_id) for case_id in case_ids)
    if len(run_ids) != len(set(run_ids)):
        _fail("closure_denominator_invalid")

    interpretation = _exact(
        plan.get("telemetry_interpretation"),
        _TELEMETRY_INTERPRETATION_FIELDS,
        "closure_denominator_invalid",
    )
    binding: dict[str, Any] = {}
    for field in _DENOMINATOR_PLAN_BINDING_FIELDS:
        value = denominator.get(field)
        if type(value) is not str or not value:
            _fail("closure_denominator_invalid")
        binding[field] = value
    for field in _TELEMETRY_INTERPRETATION_FIELDS:
        value = interpretation.get(field)
        if type(value) is not str or not value:
            _fail("closure_denominator_invalid")
        binding[field] = value

    topology = _exact(
        plan.get("campaign_topology"),
        _CAMPAIGN_TOPOLOGY_FIELDS,
        "closure_denominator_invalid",
    )
    planned_set_commitment = _domain_hash(
        _AUDIT_RUN_SET_DOMAIN, b"planned", canonical_json_bytes(list(run_ids))
    )
    if (
        topology.get("campaign_id") != campaign_id
        or topology.get("audit_run_id_derivation")
        != "sha256-domain-separated-envelope-id-and-case-handle-v1"
        or topology.get("audit_run_set_commitment_sha256")
        != planned_set_commitment
        or topology.get("one_audit_run_per_case_handle") is not True
        or not _is_sha256(topology.get("case_handle_set_commitment_sha256"))
        or not _is_sha256(topology.get("case_handle_order_commitment_sha256"))
    ):
        _fail("closure_denominator_invalid")

    denominator_commitment = _hash_canonical(denominator)
    if plan.get("denominator_plan_commitment_sha256") != denominator_commitment:
        _fail("closure_denominator_invalid")
    external_body = dict(plan)
    external_commitment = external_body.pop("external_plan_binding_sha256", None)
    if (
        not _is_sha256(external_commitment)
        or external_commitment
        != _domain_hash(_RUNTIME_PLAN_DOMAIN, canonical_json_bytes(external_body))
    ):
        _fail("closure_denominator_invalid")

    limits = _exact(
        plan.get("transport_limits"),
        _TRANSPORT_LIMIT_FIELDS,
        "closure_denominator_invalid",
    )
    max_turns = denominator.get("max_turns_per_case")
    request_cap = denominator.get("total_model_request_cap")
    network_cap = limits.get("network_attempt_cap")
    if (
        not _safe_int(max_turns, maximum=8)
        or max_turns < 1
        or not _safe_int(request_cap, maximum=_MAX_ATTEMPTS)
        or request_cap < len(case_ids)
        or request_cap > len(case_ids) * max_turns
        or not _safe_int(network_cap, maximum=_MAX_ATTEMPTS)
        or network_cap != request_cap
        or type(limits.get("concurrency")) is not int
        or limits.get("concurrency") != 1
        or limits.get("resume") is not False
        or limits.get("fallback") is not False
        or type(limits.get("tools")) is not int
        or limits.get("tools") != 0
        or not _safe_int(limits.get("request_timeout_seconds"), maximum=300)
        or limits.get("request_timeout_seconds") < 1
        or not _safe_int(limits.get("total_timeout_seconds"), maximum=7200)
        or limits.get("total_timeout_seconds") < 1
    ):
        _fail("closure_denominator_invalid")

    budget = _exact(
        plan.get("budget_policy"), _BUDGET_FIELDS, "closure_budget_invalid"
    )
    budget_body = dict(budget)
    budget_commitment = budget_body.pop("budget_policy_commitment_sha256", None)
    if (
        not _is_sha256(budget_commitment)
        or budget_commitment
        != _domain_hash(
            _RUNTIME_PLAN_DOMAIN,
            b"budget_policy",
            canonical_json_bytes(budget_body),
        )
    ):
        _fail("closure_budget_invalid")
    input_limit = budget.get("input_token_limit_total")
    output_per_request = budget.get("output_token_limit_per_request")
    output_limit = budget.get("output_token_limit_total")
    if (
        not _safe_int(input_limit)
        or input_limit < 1
        or not _safe_int(output_per_request, maximum=8192)
        or output_per_request < 1
        or not _safe_int(output_limit)
        or output_limit < 1
        or output_limit > output_per_request * request_cap
        or budget.get("currency") != "CNY"
        or budget.get("token_price_unit") != "per_million_tokens"
        or budget.get("cache_discount_assumed") is not False
        or budget.get("request_count_and_output_caps_enforced_pre_send") is not True
        or budget.get("input_token_limit_enforcement")
        != "post_response_observed_stop"
        or budget.get("cost_stop_enforcement") != "post_response_observed_stop"
        or budget.get("single_in_flight_request_may_overshoot_observed_limits")
        is not True
        or budget.get("provider_invoice_hard_cap") is not False
        or budget.get("provider_side_retention_verified") is not False
    ):
        _fail("closure_budget_invalid")
    input_price = _decimal_money(budget.get("input_price_per_million_cny"))
    output_price = _decimal_money(budget.get("output_price_per_million_cny"))
    cost_stop = _decimal_money(budget.get("local_observed_cost_stop_cny"))
    worst_case = _exact_cost(
        input_limit, output_limit, input_price, output_price
    )
    if worst_case > cost_stop:
        _fail("closure_budget_invalid")

    return _Plan(
        raw=plan,
        denominator=denominator,
        binding=binding,
        case_ids=case_ids,
        run_ids=run_ids,
        provider_id=binding["provider_id"],
        api_surface=binding["api_surface"],
        transport_id=binding["transport_id"],
        adapter_version=binding["adapter_version"],
        external_plan_binding_sha256=external_commitment,
        network_attempt_cap=network_cap,
        max_turns_per_case=max_turns,
        total_model_request_cap=request_cap,
        output_token_limit_per_request=output_per_request,
        input_token_limit_total=input_limit,
        output_token_limit_total=output_limit,
        input_price=input_price,
        output_price=output_price,
        cost_stop=cost_stop,
    )


def _validate_index_and_run_set(
    audit_index: Mapping[str, Any],
    run_rows: tuple[dict[str, Any], ...],
    *,
    plan: _Plan,
) -> tuple[dict[str, Any], tuple[dict[str, Any], ...]]:
    if type(audit_index) is not dict:
        _fail("closure_audit_run_set_invalid")
    index = audit_index
    _exact(index, _INDEX_FIELDS, "closure_audit_run_set_invalid")
    raw_entries = index.get("runs")
    if (
        index.get("schema_version") != "1.1"
        or type(raw_entries) is not list
        or len(raw_entries) > len(plan.case_ids)
    ):
        _fail("closure_audit_run_set_invalid")
    entries: list[dict[str, Any]] = []
    for ordinal, raw_entry in enumerate(raw_entries):
        entry = _exact(raw_entry, _INDEX_RUN_FIELDS, "closure_audit_run_set_invalid")
        if (
            entry.get("task_id") != plan.case_ids[ordinal]
            or entry.get("run_id") != plan.run_ids[ordinal]
            or entry.get("provider") != plan.provider_id
            or entry.get("transport") != plan.transport_id
        ):
            _fail("closure_audit_run_set_invalid")
        entries.append(entry)

    row_by_id: dict[str, dict[str, Any]] = {}
    for row in run_rows:
        _exact(row, _RUN_ROW_FIELDS, "closure_audit_run_set_invalid")
        run_id = row.get("run_id")
        if type(run_id) is not str or run_id in row_by_id:
            _fail("closure_audit_run_set_invalid")
        row_by_id[run_id] = row
    observed_ids = tuple(entry["run_id"] for entry in entries)
    if set(row_by_id) != set(observed_ids) or len(row_by_id) != len(observed_ids):
        _fail("closure_audit_run_set_invalid")
    ordered_rows = tuple(row_by_id[run_id] for run_id in observed_ids)
    for row in ordered_rows:
        status = row.get("status")
        terminal_error = row.get("terminal_error_code")
        terminal_matches_status = (
            status == "completed" and terminal_error is None
        ) or (
            status == "cancelled"
            and type(terminal_error) is str
            and terminal_error in _CASE_RUN_TERMINAL_ERROR_CODES
            and terminal_error.endswith("_cancelled")
        ) or (
            status == "failed"
            and type(terminal_error) is str
            and terminal_error in _CASE_RUN_TERMINAL_ERROR_CODES
            and not terminal_error.endswith("_cancelled")
        )
        if (
            row.get("mode") != "provider_completion_external_closure"
            or type(status) is not str
            or status not in {"completed", "failed", "cancelled"}
            or not _is_sha256(row.get("request_sha256"))
            or row.get("dataset_sha256") is not None
            or type(row.get("created_at_utc")) is not str
            or type(row.get("updated_at_utc")) is not str
            or not terminal_matches_status
        ):
            _fail("closure_audit_run_set_invalid")
    return index, ordered_rows


def _audit_event_hash(row: Mapping[str, Any]) -> str:
    body = {
        "run_id": row["run_id"],
        "sequence": row["sequence"],
        "event_type": row["event_type"],
        "occurred_at_utc": row["occurred_at_utc"],
        "actor_kind": row["actor_kind"],
        "safe_payload_json": row["safe_payload_json"],
        "prev_hash": row["prev_hash"],
    }
    return hashlib.sha256(
        _AUDIT_EVENT_DOMAIN + b"\x00" + canonical_json_bytes(body)
    ).hexdigest()


def _validate_raw_chains(
    event_rows: tuple[dict[str, Any], ...],
) -> tuple[
    dict[str, tuple[dict[str, Any], ...]],
    dict[str, dict[str, Any]],
]:
    """Recompute raw hash chains before any lower-priority semantic gate."""

    by_run: dict[str, list[dict[str, Any]]] = {}
    event_ids: set[int] = set()
    for row in event_rows:
        _exact(row, _EVENT_ROW_FIELDS, "closure_audit_chain_invalid")
        event_id = row.get("event_id")
        run_id = row.get("run_id")
        if (
            not _safe_int(event_id)
            or event_id < 1
            or event_id in event_ids
            or type(run_id) is not str
            or not run_id
            or type(row.get("sequence")) is not int
        ):
            _fail("closure_audit_chain_invalid")
        event_ids.add(event_id)
        by_run.setdefault(run_id, []).append(row)

    ordered_by_run: dict[str, tuple[dict[str, Any], ...]] = {}
    expected_chains: dict[str, dict[str, Any]] = {}
    for run_id, raw_rows in by_run.items():
        rows = sorted(raw_rows, key=lambda item: item.get("sequence", -1))
        if len(rows) > 10_000:
            _fail("closure_audit_chain_invalid")
        previous = _ZERO_HASH
        for expected_sequence, row in enumerate(rows, start=1):
            if (
                type(row.get("sequence")) is not int
                or row.get("sequence") != expected_sequence
                or type(row.get("event_type")) is not str
                or type(row.get("occurred_at_utc")) is not str
                or type(row.get("actor_kind")) is not str
                or type(row.get("safe_payload_json")) is not str
                or row.get("prev_hash") != previous
                or not _is_sha256(row.get("event_hash"))
                or row.get("event_hash") != _audit_event_hash(row)
            ):
                _fail("closure_audit_chain_invalid")
            previous = row["event_hash"]
        ordered_by_run[run_id] = tuple(rows)
        expected_chains[run_id] = {
            "valid": True,
            "run_id": run_id,
            "event_count": len(rows),
            "chain_head": previous,
            "error_code": None,
            "error_sequence": None,
        }
    return ordered_by_run, expected_chains


def _validate_reported_chains(
    index: Mapping[str, Any],
    *,
    expected_chains: Mapping[str, Mapping[str, Any]],
) -> None:
    """Validate index chain projections without assuming plan-prefix validity."""

    if type(index) is not dict:
        _fail("closure_audit_run_set_invalid")
    entries = index.get("runs")
    if type(entries) is not list:
        _fail("closure_audit_run_set_invalid")
    for entry in entries:
        if type(entry) is not dict:
            _fail("closure_audit_chain_invalid")
        run_id = entry.get("run_id")
        if type(run_id) is not str or not run_id:
            _fail("closure_audit_chain_invalid")
        expected_chain = expected_chains.get(
            run_id,
            {
                "valid": True,
                "run_id": run_id,
                "event_count": 0,
                "chain_head": _ZERO_HASH,
                "error_code": None,
                "error_sequence": None,
            },
        )
        chain = entry.get("chain_verification")
        if (
            type(chain) is not dict
            or set(chain) != _CHAIN_FIELDS
            or chain.get("valid") is not True
            or type(chain.get("event_count")) is not int
            or chain.get("error_code") is not None
            or chain.get("error_sequence") is not None
            or not _json_equal(chain, expected_chain)
        ):
            _fail("closure_audit_chain_invalid")


def _decode_event_runs(
    raw_by_run: Mapping[str, tuple[dict[str, Any], ...]],
    *,
    ordered_run_rows: tuple[dict[str, Any], ...],
) -> tuple[tuple[_Event, ...], ...]:
    observed_ids = tuple(row["run_id"] for row in ordered_run_rows)
    if not set(raw_by_run).issubset(observed_ids):
        _fail("closure_audit_run_set_invalid")
    result: list[tuple[_Event, ...]] = []
    for run_id in observed_ids:
        decoded: list[_Event] = []
        for row in raw_by_run.get(run_id, ()):
            try:
                payload_bytes = row["safe_payload_json"].encode(
                    "utf-8", errors="strict"
                )
                payload = decode_strict_json_object(
                    payload_bytes, max_bytes=_MAX_EVENT_PAYLOAD_BYTES
                )
                if canonical_json_bytes(payload) != payload_bytes:
                    _fail("closure_event_projection_invalid")
            except ExternalClosurePrimitiveError as error:
                if error.code == "closure_event_projection_invalid":
                    raise
                _fail("closure_event_projection_invalid")
            decoded.append(_Event(row=row, payload=payload))
        result.append(tuple(decoded))
    return tuple(result)


def _validate_event_skeleton(
    decoded_event_runs: tuple[tuple[_Event, ...], ...],
) -> None:
    """Validate event grammar/actors/shapes before denominator cross-binding."""

    actors = {
        "run_started": "system",
        "run_status_changed": "system",
        "model_call_recorded": "agent_sdk",
        "model_run_usage_recorded": "agent_sdk",
        "model_request_started": "provider_adapter",
        "provider_transport_request_sent": "provider_adapter",
        **{event_type: "provider_adapter" for event_type in _TERMINAL_EVENT_TYPES},
    }
    for events in decoded_event_runs:
        if not events:
            _fail("closure_event_projection_invalid")
        for event in events:
            event_type = event.row["event_type"]
            if (
                event_type not in _ALLOWED_EVENT_TYPES
                or event.row["actor_kind"] != actors[event_type]
            ):
                _fail("closure_event_projection_invalid")
            if event_type == "run_started":
                fields = _RUN_STARTED_FIELDS
            elif event_type == "model_request_started":
                fields = _START_FIELDS
            elif event_type == "provider_transport_request_sent":
                fields = _SEND_FIELDS
            elif event_type in {
                "model_response_telemetry_recorded",
                "provider_completion_mapping_unmapped",
            }:
                fields = _TERMINAL_FIELDS | {"completion_record"}
            elif event_type in _TERMINAL_EVENT_TYPES:
                fields = _TERMINAL_FIELDS
            elif event_type == "model_call_recorded":
                fields = _MODEL_CALL_PAYLOAD_FIELDS
            elif event_type == "model_run_usage_recorded":
                fields = _MODEL_USAGE_FIELDS
            else:
                fields = _RUN_STATUS_FIELDS
            _exact(event.payload, frozenset(fields), "closure_event_projection_invalid")

        if events[0].row["event_type"] != "run_started":
            _fail("closure_event_projection_invalid")
        cursor = 1
        while (
            cursor < len(events)
            and events[cursor].row["event_type"] == "model_request_started"
        ):
            cursor += 1
            if (
                cursor < len(events)
                and events[cursor].row["event_type"]
                == "provider_transport_request_sent"
            ):
                cursor += 1
            if (
                cursor >= len(events)
                or events[cursor].row["event_type"] not in _TERMINAL_EVENT_TYPES
            ):
                _fail("closure_event_projection_invalid")
            cursor += 1
        while (
            cursor < len(events)
            and events[cursor].row["event_type"] == "model_call_recorded"
        ):
            cursor += 1
        if (
            cursor >= len(events)
            or events[cursor].row["event_type"] != "model_run_usage_recorded"
        ):
            _fail("closure_event_projection_invalid")
        cursor += 1
        if (
            cursor >= len(events)
            or events[cursor].row["event_type"] != "run_status_changed"
            or cursor + 1 != len(events)
        ):
            _fail("closure_event_projection_invalid")


def _prevalidate_bridge_commitments(
    index: Mapping[str, Any],
    decoded_event_runs: tuple[tuple[_Event, ...], ...],
    *,
    plan: _Plan,
) -> None:
    for ordinal, events in enumerate(decoded_event_runs):
        started = sorted(
            (
                {
                    "attempt_index": event.payload.get("attempt_index"),
                    "event_hash": event.row["event_hash"],
                }
                for event in events
                if event.row["event_type"] == "model_request_started"
            ),
            key=lambda item: (
                item["attempt_index"]
                if type(item["attempt_index"]) is int
                else -1
            ),
        )
        terminals = sorted(
            (
                {
                    "attempt_index": event.payload.get("attempt_index"),
                    "event_hash": event.row["event_hash"],
                }
                for event in events
                if event.row["event_type"] in _TERMINAL_EVENT_TYPES
            ),
            key=lambda item: (
                item["attempt_index"]
                if type(item["attempt_index"]) is int
                else -1
            ),
        )
        if any(
            type(item["attempt_index"]) is not int
            or not _safe_int(item["attempt_index"], maximum=_MAX_ATTEMPTS - 1)
            for item in (*started, *terminals)
        ):
            _fail("closure_event_projection_invalid")
        bridge = index["runs"][ordinal].get(
            "completion_telemetry_event_commitment"
        )
        if (
            type(bridge) is not dict
            or set(bridge) != _BRIDGE_FIELDS
            or type(bridge.get("write_failed")) is not bool
        ):
            _fail("closure_event_projection_invalid")
        body = {
            "schema_version": "provider-completion-ledger-bridge-commitment/1.0",
            "case_id": plan.case_ids[ordinal],
            "binding_sha256": _hash_canonical(plan.binding),
            "started": started,
            "terminals": terminals,
            "all_started_attempts_terminal": (
                {item["attempt_index"] for item in started}
                == {item["attempt_index"] for item in terminals}
            ),
            "write_failed": bridge["write_failed"],
        }
        expected = {**body, "commitment_sha256": _hash_canonical(body)}
        if not _json_equal(bridge, expected):
            _fail("closure_event_projection_invalid")


def _prevalidate_model_call_rows(
    rows: tuple[dict[str, Any], ...],
    *,
    ordered_run_rows: tuple[dict[str, Any], ...],
    plan: _Plan,
    model_id: str,
) -> None:
    expected_run_ids = {row["run_id"] for row in ordered_run_rows}
    model_ids: set[str] = set()
    for row in rows:
        _validate_model_call_row(row, plan=plan, model_id=model_id)
        if (
            row["run_id"] not in expected_run_ids
            or row["model_call_id"] in model_ids
        ):
            _fail("closure_event_projection_invalid")
        model_ids.add(row["model_call_id"])


def _prevalidate_event_projections(
    decoded: tuple[tuple[_Event, ...], ...],
    *,
    ordered_runs: tuple[dict[str, Any], ...],
    model_calls: tuple[dict[str, Any], ...],
    plan: _Plan,
    campaign_id: str,
    authorization_grant_sha256: str,
    api_origin: str,
    api_path: str,
    model_id: str,
    persisted_evidence_context: object,
) -> None:
    """Check independent event defects before a malformed denominator masks them."""

    next_attempt_index = 0
    next_response_index = 0
    next_network_index = 0
    for ordinal, (run, events) in enumerate(zip(ordered_runs, decoded)):
        if not _json_equal(events[0].payload, {
            "mode": "provider_completion_external_closure",
            "request_sha256": run["request_sha256"],
            "dataset_sha256": None,
        }) or not _json_equal(events[-1].payload, {
            "from": "running",
            "to": run["status"],
            "error_code": run["terminal_error_code"],
        }):
            _fail("closure_event_projection_invalid")
        model_by_id = {
            row["model_call_id"]: row for row in model_calls
            if row["run_id"] == run["run_id"]
        }
        model_events = tuple(
            event for event in events if event.row["event_type"] == "model_call_recorded"
        )
        ordered_model_rows: list[dict[str, Any]] = []
        for event in model_events:
            model_call_id = event.payload.get("model_call_id")
            if type(model_call_id) is not str or model_call_id not in model_by_id:
                _fail("closure_event_projection_invalid")
            row = model_by_id.pop(model_call_id)
            if not _json_equal(event.payload, {
                field: row[field] for field in _MODEL_CALL_PAYLOAD_FIELDS
            }):
                _fail("closure_event_projection_invalid")
            ordered_model_rows.append(row)
        if model_by_id:
            _fail("closure_event_projection_invalid")
        _validate_model_usage_payload(
            events[-2].payload,
            model_rows=ordered_model_rows,
            response_detail_count=sum(
                event.row["event_type"] in {
                    "model_response_telemetry_recorded",
                    "provider_completion_mapping_unmapped",
                    "model_response_telemetry_rejected",
                }
                for event in events
            ),
            plan=plan,
        )
        cursor = 1
        case_attempt_index = 0
        while events[cursor].row["event_type"] == "model_request_started":
            start = events[cursor].payload
            attempt_index = start.get("attempt_index")
            if (
                not _safe_int(attempt_index, maximum=_MAX_ATTEMPTS - 1)
                or attempt_index != next_attempt_index
            ):
                _fail("closure_event_projection_invalid")
            next_attempt_index += 1
            expected_start = {
                "schema_version": "provider-completion-ledger-event/1.1",
                "case_id": plan.case_ids[ordinal],
                "attempt_index": attempt_index,
                "case_attempt_index": case_attempt_index,
                "binding": plan.binding,
            }
            if not _json_equal(start, expected_start):
                _fail("closure_event_projection_invalid")
            cursor += 1
            sent = events[cursor].row["event_type"] == "provider_transport_request_sent"
            if sent:
                _validate_send_payload(
                    events[cursor].payload,
                    expected_start=start,
                    plan=plan,
                    campaign_id=campaign_id,
                    authorization_grant_sha256=authorization_grant_sha256,
                    api_origin=api_origin,
                    api_path=api_path,
                    model_id=model_id,
                )
                if (
                    events[cursor].payload["audit_request_sha256"] != run["request_sha256"]
                    or events[cursor].payload["network_call_index"] != next_network_index
                ):
                    _fail("closure_event_projection_invalid")
                next_network_index += 1
                cursor += 1
            terminal = events[cursor]
            event_type = terminal.row["event_type"]
            terminal_kind = terminal.payload.get("terminal_kind")
            error_code = terminal.payload.get("error_code")
            if (
                type(terminal_kind) is not str
                or (error_code is not None and type(error_code) is not str)
                or error_code not in _TERMINAL_ERROR_CODES[event_type]
                or sent != (event_type in _SEND_REQUIRED_EVENTS)
                or any(not _json_equal(terminal.payload.get(field), value)
                       for field, value in expected_start.items())
            ):
                _fail("closure_event_projection_invalid")
            expected_type = _TERMINAL_EVENT_BY_KIND.get(terminal_kind)
            if terminal_kind == "response_accepted":
                record = terminal.payload.get("completion_record")
                try:
                    validate_persisted_live_completion_record(
                        record, context=persisted_evidence_context,  # type: ignore[arg-type]
                    )
                except Exception:
                    _fail("closure_event_projection_invalid")
                if record["normalized_completion_state"] == "unmapped":
                    expected_type = "provider_completion_mapping_unmapped"
                if (
                    record.get("response_index") != terminal.payload.get("response_index")
                    or record.get("request_index") != attempt_index
                ):
                    _fail("closure_event_projection_invalid")
            if expected_type != event_type:
                _fail("closure_event_projection_invalid")
            response_index = terminal.payload.get("response_index")
            has_response_index = terminal_kind in {
                "response_accepted", "response_rejected"
            }
            if (has_response_index and not _safe_int(response_index)) or (
                not has_response_index and response_index is not None
            ):
                _fail("closure_event_projection_invalid")
            if has_response_index:
                if response_index != next_response_index:
                    _fail("closure_event_projection_invalid")
                next_response_index += 1
            cursor += 1
            case_attempt_index += 1


def _validate_model_call_row(
    row: dict[str, Any], *, plan: _Plan, model_id: str
) -> None:
    _exact(row, _MODEL_CALL_ROW_FIELDS, "closure_event_projection_invalid")
    latency = row.get("latency_ms")
    if (
        type(row.get("model_call_id")) is not str
        or _MODEL_CALL_ID.fullmatch(row["model_call_id"]) is None
        or type(row.get("run_id")) is not str
        or row.get("provider") != plan.provider_id
        or row.get("model") != model_id
        or type(row.get("started_at_utc")) is not str
        or type(latency) not in {int, float}
        or not math.isfinite(latency)
        or not 0 <= latency <= 600_000
        or row.get("cost_usd") is not None
        or type(row.get("outcome")) is not str
        or row.get("outcome") not in {"succeeded", "failed"}
        or (
            row.get("outcome") == "succeeded"
            and row.get("error_code") is not None
        )
        or (
            row.get("outcome") == "failed"
            and (
                type(row.get("error_code")) is not str
                or _SAFE_ERROR.fullmatch(row["error_code"]) is None
            )
        )
    ):
        _fail("closure_event_projection_invalid")
    for field in ("input_tokens", "output_tokens", "cached_tokens"):
        value = row.get(field)
        if value is not None and (type(value) is not int or value < 0):
            _fail("closure_event_projection_invalid")


def _validate_model_usage_payload(
    payload: dict[str, Any],
    *,
    model_rows: Sequence[dict[str, Any]],
    response_detail_count: int,
    plan: _Plan,
) -> None:
    _exact(payload, _MODEL_USAGE_FIELDS, "closure_event_projection_invalid")
    for field in (
        "request_count",
        "input_unit_count",
        "output_unit_count",
        "total_unit_count",
        "cached_input_unit_count",
    ):
        value = payload.get(field)
        if value is not None and (type(value) is not int or value < 0):
            _fail("closure_event_projection_invalid")
    if (
        type(payload.get("usage_complete")) is not bool
        or not _safe_int(payload.get("response_detail_count"), maximum=_MAX_ATTEMPTS)
        or not _safe_int(payload.get("model_call_rows_recorded"), maximum=_MAX_ATTEMPTS)
        or payload.get("response_detail_count") != response_detail_count
        or payload.get("model_call_rows_recorded") != len(model_rows)
        or payload.get("estimated_cost_usd") is not None
        or payload.get("latency_allocation") != "equal_share_of_agent_segment"
        or payload.get("model_call_cost_method") != "cny_recomputed_only_in_closure"
        or payload.get("provider") != plan.provider_id
        or payload.get("transport") != plan.transport_id
    ):
        _fail("closure_event_projection_invalid")

    input_complete = bool(model_rows) and all(
        type(row["input_tokens"]) is int for row in model_rows
    )
    output_complete = bool(model_rows) and all(
        type(row["output_tokens"]) is int for row in model_rows
    )
    cached_complete = bool(model_rows) and all(
        type(row["cached_tokens"]) is int for row in model_rows
    )
    usage_complete = input_complete and output_complete and cached_complete
    input_total = (
        sum(row["input_tokens"] for row in model_rows) if input_complete else None
    )
    output_total = (
        sum(row["output_tokens"] for row in model_rows) if output_complete else None
    )
    total = (
        input_total + output_total
        if input_total is not None and output_total is not None
        else None
    )
    cached_total = (
        sum(row["cached_tokens"] for row in model_rows)
        if cached_complete
        else None
    )
    expected = {
        "usage_complete": usage_complete,
        "request_count": len(model_rows) if model_rows else None,
        "input_unit_count": input_total,
        "output_unit_count": output_total,
        "total_unit_count": total,
        "cached_input_unit_count": cached_total,
    }
    if any(payload.get(field) != value for field, value in expected.items()):
        _fail("closure_event_projection_invalid")


def _validate_response_model_rows(
    *,
    run_ordinal: int,
    model_rows: Sequence[dict[str, Any]],
    denominator_summary: Any,
) -> bool:
    cases = {
        item.case_ordinal: item for item in denominator_summary.cases
    }
    case = cases.get(run_ordinal)
    if case is None:
        if model_rows:
            _fail("closure_event_projection_invalid")
        return True
    usage_groups = tuple(case.sdk_request_usage_indices_by_response)
    expected_count = sum(len(item.sdk_request_usage_indices) for item in usage_groups)
    if expected_count != len(model_rows):
        _fail("closure_event_projection_invalid")
    responses = {
        item.response_index: item for item in denominator_summary.responses
    }
    coverage_complete = True
    offset = 0
    for group in usage_groups:
        size = len(group.sdk_request_usage_indices)
        rows = model_rows[offset : offset + size]
        offset += size
        response = responses.get(group.response_index)
        if response is None:
            coverage_complete = False
            continue
        if response.requests is not None and response.requests != size:
            _fail("closure_event_projection_invalid")
        for response_value, field in (
            (response.input_tokens, "input_tokens"),
            (response.output_tokens, "output_tokens"),
            (response.cached_input_tokens, "cached_tokens"),
        ):
            row_values = [row[field] for row in rows]
            if response_value is None:
                if any(value is not None for value in row_values):
                    _fail("closure_event_projection_invalid")
            elif (
                any(type(value) is not int for value in row_values)
                or sum(row_values) != response_value
            ):
                _fail("closure_event_projection_invalid")
        if response.usage_complete and any(
            row["outcome"] != "succeeded" or row["error_code"] is not None
            for row in rows
        ):
            _fail("closure_event_projection_invalid")
    return coverage_complete


def _expected_terminal_payload(
    attempt: Any,
    *,
    case_id: str,
    binding: Mapping[str, Any],
    records_by_response: Mapping[int, Mapping[str, Any]],
) -> tuple[str, dict[str, Any]]:
    event_type = _TERMINAL_EVENT_BY_KIND.get(attempt.terminal_kind)
    if event_type is None:
        _fail("closure_event_projection_invalid")
    payload: dict[str, Any] = {
        "schema_version": "provider-completion-ledger-event/1.1",
        "case_id": case_id,
        "attempt_index": attempt.attempt_index,
        "case_attempt_index": attempt.case_attempt_index,
        "terminal_kind": attempt.terminal_kind,
        "response_index": attempt.response_index,
        "error_code": attempt.error_code,
        "binding": dict(binding),
    }
    if attempt.terminal_kind == "response_accepted":
        record = records_by_response.get(attempt.response_index)
        if record is None:
            _fail("closure_event_projection_invalid")
        payload["completion_record"] = record
        if record.get("normalized_completion_state") == "unmapped":
            event_type = "provider_completion_mapping_unmapped"
    return event_type, payload


def _validate_send_payload(
    payload: dict[str, Any],
    *,
    expected_start: Mapping[str, Any],
    plan: _Plan,
    campaign_id: str,
    authorization_grant_sha256: str,
    api_origin: str,
    api_path: str,
    model_id: str,
) -> str:
    _exact(payload, _SEND_FIELDS, "closure_event_projection_invalid")
    execution_commitment = payload.get("execution_input_commitment_sha256")
    audit_request = payload.get("audit_request_sha256")
    if (
        payload.get("schema_version") != "provider-completion-transport-send/1.0"
        or payload.get("case_id") != expected_start["case_id"]
        or type(payload.get("attempt_index")) is not int
        or payload.get("attempt_index") != expected_start["attempt_index"]
        or type(payload.get("case_attempt_index")) is not int
        or payload.get("case_attempt_index") != expected_start["case_attempt_index"]
        or not _safe_int(payload.get("network_call_index"), maximum=_MAX_ATTEMPTS - 1)
        or payload.get("provider_id") != plan.provider_id
        or payload.get("api_surface") != plan.api_surface
        or payload.get("transport_id") != plan.transport_id
        or payload.get("adapter_version") != plan.adapter_version
        or payload.get("method") != "POST"
        or payload.get("origin") != api_origin
        or payload.get("path") != api_path
        or type(payload.get("requested_output_token_cap")) is not int
        or payload.get("requested_output_token_cap")
        != plan.output_token_limit_per_request
        or not _is_sha256(execution_commitment)
        or not _is_sha256(audit_request)
        or payload.get("external_plan_binding_sha256")
        != plan.external_plan_binding_sha256
        or payload.get("authorization_grant_sha256")
        != authorization_grant_sha256
        or payload.get("headers_persisted") is not False
        or payload.get("request_body_persisted") is not False
        or payload.get("task_content_persisted") is not False
    ):
        _fail("closure_event_projection_invalid")
    request_projection = {
        "campaign_id": campaign_id,
        "case_handle": expected_start["case_id"],
        "execution_input_commitment_sha256": execution_commitment,
        "external_plan_binding_sha256": plan.external_plan_binding_sha256,
        "authorization_grant_sha256": authorization_grant_sha256,
        "provider_id": plan.provider_id,
        "model_id": model_id,
        "api_surface": plan.api_surface,
        "transport_id": plan.transport_id,
    }
    if audit_request != _hash_canonical(request_projection):
        _fail("closure_event_projection_invalid")
    return execution_commitment


def _validate_events(
    *,
    index: Mapping[str, Any],
    ordered_run_rows: tuple[dict[str, Any], ...],
    decoded_event_runs: tuple[tuple[_Event, ...], ...],
    model_call_rows: tuple[dict[str, Any], ...],
    denominator_summary: Any,
    denominator_artifact: Mapping[str, Any],
    plan: _Plan,
    campaign_id: str,
    authorization_grant_sha256: str,
    execution_input_order_commitment_sha256: str,
    api_origin: str,
    api_path: str,
    model_id: str,
) -> _EventAnalysis:
    if (
        type(model_id) is not str
        or _SAFE_IDENTIFIER.fullmatch(model_id) is None
        or type(api_origin) is not str
        or not api_origin
        or type(api_path) is not str
        or not api_path.startswith("/")
        or not _is_sha256(authorization_grant_sha256)
        or not _is_sha256(execution_input_order_commitment_sha256)
    ):
        _fail("closure_event_projection_invalid")

    model_by_run: dict[str, list[dict[str, Any]]] = {
        row["run_id"]: [] for row in ordered_run_rows
    }
    model_ids: set[str] = set()
    for row in model_call_rows:
        _validate_model_call_row(row, plan=plan, model_id=model_id)
        if row["run_id"] not in model_by_run or row["model_call_id"] in model_ids:
            _fail("closure_event_projection_invalid")
        model_ids.add(row["model_call_id"])
        model_by_run[row["run_id"]].append(row)

    records_value = denominator_artifact.get("records")
    if type(records_value) is not list:
        _fail("closure_denominator_invalid")
    records_by_response: dict[int, Mapping[str, Any]] = {}
    for record in records_value:
        if type(record) is not dict or type(record.get("response_index")) is not int:
            _fail("closure_denominator_invalid")
        response_index = record["response_index"]
        if response_index in records_by_response:
            _fail("closure_denominator_invalid")
        records_by_response[response_index] = record

    expected_attempts = {
        item.attempt_index: item for item in denominator_summary.attempts
    }
    if len(expected_attempts) != denominator_summary.attempt_count:
        _fail("closure_denominator_invalid")
    seen_attempts: set[int] = set()
    seen_terminals: set[int] = set()
    event_counts: Counter[str] = Counter()
    network_indices: list[int] = []
    execution_commitments: list[str] = []
    run_analyses: list[_RunEvents] = []
    accepted_events = 0
    rejected_events = 0
    ledger_failure = False
    record_usage_index_coverage_complete = True

    for ordinal, (run, events) in enumerate(
        zip(ordered_run_rows, decoded_event_runs)
    ):
        if not events or any(event.row["event_type"] not in _ALLOWED_EVENT_TYPES for event in events):
            _fail("closure_event_projection_invalid")
        for event in events:
            event_counts[event.row["event_type"]] += 1
        first = events[0]
        if (
            first.row["event_type"] != "run_started"
            or first.row["actor_kind"] != "system"
            or set(first.payload) != _RUN_STARTED_FIELDS
            or not _json_equal(
                first.payload,
                {
                    "mode": "provider_completion_external_closure",
                    "request_sha256": run["request_sha256"],
                    "dataset_sha256": None,
                },
            )
        ):
            _fail("closure_event_projection_invalid")

        cursor = 1
        attempt_groups: list[_AttemptEvents] = []
        case_execution_commitment: str | None = None
        while cursor < len(events) and events[cursor].row["event_type"] == "model_request_started":
            started = events[cursor]
            if started.row["actor_kind"] != "provider_adapter":
                _fail("closure_event_projection_invalid")
            start_payload = _exact(
                started.payload, _START_FIELDS, "closure_event_projection_invalid"
            )
            attempt_index = start_payload.get("attempt_index")
            attempt = expected_attempts.get(attempt_index)
            expected_case_id = plan.case_ids[ordinal]
            expected_start = {
                "schema_version": "provider-completion-ledger-event/1.1",
                "case_id": expected_case_id,
                "attempt_index": attempt_index,
                "case_attempt_index": (
                    attempt.case_attempt_index if attempt is not None else None
                ),
                "binding": plan.binding,
            }
            if (
                attempt is None
                or attempt.case_ordinal != ordinal
                or type(start_payload.get("attempt_index")) is not int
                or type(start_payload.get("case_attempt_index")) is not int
                or attempt_index in seen_attempts
                or not _json_equal(start_payload, expected_start)
            ):
                _fail("closure_event_projection_invalid")
            seen_attempts.add(attempt_index)
            cursor += 1

            sent: _Event | None = None
            if cursor < len(events) and events[cursor].row["event_type"] == "provider_transport_request_sent":
                sent = events[cursor]
                if sent.row["actor_kind"] != "provider_adapter":
                    _fail("closure_event_projection_invalid")
                execution_commitment = _validate_send_payload(
                    sent.payload,
                    expected_start=start_payload,
                    plan=plan,
                    campaign_id=campaign_id,
                    authorization_grant_sha256=authorization_grant_sha256,
                    api_origin=api_origin,
                    api_path=api_path,
                    model_id=model_id,
                )
                if (
                    case_execution_commitment is not None
                    and execution_commitment != case_execution_commitment
                ):
                    _fail("closure_event_projection_invalid")
                case_execution_commitment = execution_commitment
                network_indices.append(sent.payload["network_call_index"])
                cursor += 1

            if cursor >= len(events) or events[cursor].row["event_type"] not in _TERMINAL_EVENT_TYPES:
                _fail("closure_event_projection_invalid")
            terminal = events[cursor]
            if terminal.row["actor_kind"] != "provider_adapter":
                _fail("closure_event_projection_invalid")
            expected_event_type, expected_terminal = _expected_terminal_payload(
                attempt,
                case_id=expected_case_id,
                binding=plan.binding,
                records_by_response=records_by_response,
            )
            if (
                terminal.row["event_type"] != expected_event_type
                or type(terminal.payload.get("attempt_index")) is not int
                or type(terminal.payload.get("case_attempt_index")) is not int
                or (
                    terminal.payload.get("response_index") is not None
                    and type(terminal.payload.get("response_index")) is not int
                )
                or not _json_equal(terminal.payload, expected_terminal)
                or terminal.payload.get("error_code")
                not in _TERMINAL_ERROR_CODES[terminal.row["event_type"]]
                or attempt_index in seen_terminals
            ):
                _fail("closure_event_projection_invalid")
            seen_terminals.add(attempt_index)
            if terminal.row["event_type"] in _SEND_REQUIRED_EVENTS and sent is None:
                _fail("closure_event_projection_invalid")
            if terminal.row["event_type"] in _NO_SEND_EVENTS and sent is not None:
                _fail("closure_event_projection_invalid")
            if attempt.terminal_kind == "response_accepted":
                accepted_events += 1
            elif attempt.terminal_kind == "response_rejected":
                rejected_events += 1
            attempt_groups.append(_AttemptEvents(started, sent, terminal))
            cursor += 1

        model_events: list[_Event] = []
        while cursor < len(events) and events[cursor].row["event_type"] == "model_call_recorded":
            event = events[cursor]
            if event.row["actor_kind"] != "agent_sdk":
                _fail("closure_event_projection_invalid")
            _exact(
                event.payload,
                _MODEL_CALL_PAYLOAD_FIELDS,
                "closure_event_projection_invalid",
            )
            model_events.append(event)
            cursor += 1
        if cursor >= len(events) or events[cursor].row["event_type"] != "model_run_usage_recorded":
            _fail("closure_event_projection_invalid")
        usage_event = events[cursor]
        cursor += 1
        if (
            usage_event.row["actor_kind"] != "agent_sdk"
            or cursor >= len(events)
            or events[cursor].row["event_type"] != "run_status_changed"
            or cursor + 1 != len(events)
        ):
            _fail("closure_event_projection_invalid")
        status_event = events[cursor]
        if status_event.row["actor_kind"] != "system":
            _fail("closure_event_projection_invalid")
        _exact(status_event.payload, _RUN_STATUS_FIELDS, "closure_event_projection_invalid")
        expected_status = {
            "from": "running",
            "to": run["status"],
            "error_code": run["terminal_error_code"],
        }
        if not _json_equal(status_event.payload, expected_status):
            _fail("closure_event_projection_invalid")

        persisted_rows = model_by_run[run["run_id"]]
        event_rows_by_id = {
            event.payload.get("model_call_id"): event for event in model_events
        }
        if (
            len(event_rows_by_id) != len(model_events)
            or set(event_rows_by_id)
            != {row["model_call_id"] for row in persisted_rows}
        ):
            _fail("closure_event_projection_invalid")
        ordered_model_rows: list[dict[str, Any]] = []
        for event in model_events:
            row = next(
                item
                for item in persisted_rows
                if item["model_call_id"] == event.payload["model_call_id"]
            )
            expected_payload = {
                field: row[field] for field in _MODEL_CALL_PAYLOAD_FIELDS
            }
            if not _json_equal(event.payload, expected_payload):
                _fail("closure_event_projection_invalid")
            ordered_model_rows.append(row)
        record_usage_index_coverage_complete = (
            _validate_response_model_rows(
                run_ordinal=ordinal,
                model_rows=ordered_model_rows,
                denominator_summary=denominator_summary,
            )
            and record_usage_index_coverage_complete
        )
        response_detail_count = sum(
            attempt.terminal_kind in {"response_accepted", "response_rejected"}
            for attempt in expected_attempts.values()
            if attempt.case_ordinal == ordinal
        )
        _validate_model_usage_payload(
            usage_event.payload,
            model_rows=ordered_model_rows,
            response_detail_count=response_detail_count,
            plan=plan,
        )

        started_hashes = [
            {
                "attempt_index": group.started.payload["attempt_index"],
                "event_hash": group.started.row["event_hash"],
            }
            for group in attempt_groups
        ]
        terminal_hashes = [
            {
                "attempt_index": group.terminal.payload["attempt_index"],
                "event_hash": group.terminal.row["event_hash"],
            }
            for group in attempt_groups
        ]
        bridge = index["runs"][ordinal].get(
            "completion_telemetry_event_commitment"
        )
        if type(bridge) is not dict or set(bridge) != _BRIDGE_FIELDS:
            _fail("closure_event_projection_invalid")
        for name in ("started", "terminals"):
            items = bridge.get(name)
            if type(items) is not list or any(
                type(item) is not dict
                or set(item) != _BRIDGE_HASH_FIELDS
                or type(item.get("attempt_index")) is not int
                or not _is_sha256(item.get("event_hash"))
                for item in items
            ):
                _fail("closure_event_projection_invalid")
        if (
            type(bridge.get("all_started_attempts_terminal")) is not bool
            or type(bridge.get("write_failed")) is not bool
        ):
            _fail("closure_event_projection_invalid")
        bridge_body = {
            "schema_version": "provider-completion-ledger-bridge-commitment/1.0",
            "case_id": plan.case_ids[ordinal],
            "binding_sha256": _hash_canonical(plan.binding),
            "started": sorted(started_hashes, key=lambda item: item["attempt_index"]),
            "terminals": sorted(
                terminal_hashes, key=lambda item: item["attempt_index"]
            ),
            "all_started_attempts_terminal": (
                {item["attempt_index"] for item in started_hashes}
                == {item["attempt_index"] for item in terminal_hashes}
            ),
            "write_failed": bridge["write_failed"],
        }
        expected_bridge = {
            **bridge_body,
            "commitment_sha256": _hash_canonical(bridge_body),
        }
        if not _json_equal(bridge, expected_bridge):
            _fail("closure_event_projection_invalid")
        ledger_failure = ledger_failure or bridge["write_failed"]

        # No-send terminals carry no execution-input commitment.  Their absence
        # is a valid failure path, while every present send remains bound below.
        if case_execution_commitment is not None:
            execution_commitments.append(case_execution_commitment)
            request_projection = {
                "campaign_id": campaign_id,
                "case_handle": plan.case_ids[ordinal],
                "execution_input_commitment_sha256": case_execution_commitment,
                "external_plan_binding_sha256": plan.external_plan_binding_sha256,
                "authorization_grant_sha256": authorization_grant_sha256,
                "provider_id": plan.provider_id,
                "model_id": model_id,
                "api_surface": plan.api_surface,
                "transport_id": plan.transport_id,
            }
            if run["request_sha256"] != _hash_canonical(request_projection):
                _fail("closure_event_projection_invalid")

        run_analyses.append(
            _RunEvents(
                run=run,
                events=events,
                attempts=tuple(attempt_groups),
                model_call_events=tuple(model_events),
                usage_event=usage_event,
                status_event=status_event,
            )
        )

    if seen_attempts != set(expected_attempts) or seen_terminals != set(expected_attempts):
        _fail("closure_event_projection_invalid")
    if accepted_events != denominator_summary.accepted_response_count:
        _fail("closure_event_projection_invalid")
    if rejected_events != denominator_summary.rejected_response_count:
        _fail("closure_event_projection_invalid")
    if network_indices != list(range(len(network_indices))):
        _fail("closure_event_projection_invalid")
    transport_retry_or_cap_violation = (
        len(network_indices) > plan.network_attempt_cap
    )
    if len(execution_commitments) == len(plan.case_ids):
        observed_execution_commitment = _domain_hash(
            _EXECUTION_INPUT_ORDER_DOMAIN,
            canonical_json_bytes(execution_commitments),
        )
        if observed_execution_commitment != execution_input_order_commitment_sha256:
            _fail("closure_event_projection_invalid")

    return _EventAnalysis(
        runs=tuple(run_analyses),
        event_counts=dict(sorted(event_counts.items())),
        network_attempt_count=len(network_indices),
        model_request_count=len(seen_attempts),
        model_call_count=len(model_call_rows),
        ledger_failure_observed=ledger_failure,
        transport_retry_or_cap_violation=transport_retry_or_cap_violation,
        record_usage_index_coverage_complete=(
            record_usage_index_coverage_complete
        ),
        execution_input_commitments=tuple(execution_commitments),
    )


def _ledger_reconciliation_reasons(
    analysis: _EventAnalysis,
) -> tuple[str, ...]:
    reasons: list[str] = []
    if analysis.ledger_failure_observed:
        reasons.append("completion_telemetry_ledger_write_failed")
    if analysis.transport_retry_or_cap_violation:
        reasons.append("transport_retry_or_cap_violation")
    return _ordered_reasons(reasons)


def _validate_outer_telemetry(
    telemetry: Mapping[str, Any],
    *,
    plan: _Plan,
    denominator_summary: Any,
    analysis: _EventAnalysis,
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    value = _snapshot_mapping(telemetry, "closure_outer_projection_invalid")
    _exact(value, _OUTER_TELEMETRY_FIELDS, "closure_outer_projection_invalid")
    if (
        value.get("schema_version") != "phase6-completion-telemetry/1.0"
        or value.get("status") != "recorded"
        or value.get("historical_backfill_performed") is not False
        or not _json_equal(value.get("runtime_plan"), plan.denominator)
    ):
        _fail("closure_outer_projection_invalid")

    ledger_reasons = _ledger_reconciliation_reasons(analysis)
    expected_ledger = {
        "all_chains_valid": True,
        "ledger_export_failed": False,
        "ledger_failure_observed": analysis.ledger_failure_observed,
        "event_counts": analysis.event_counts,
        "reasons": list(ledger_reasons),
    }
    ledger = value.get("ledger_reconciliation")
    if (
        type(ledger) is not dict
        or set(ledger) != _LEDGER_RECONCILIATION_FIELDS
        or type(ledger.get("all_chains_valid")) is not bool
        or type(ledger.get("ledger_export_failed")) is not bool
        or type(ledger.get("ledger_failure_observed")) is not bool
        or type(ledger.get("event_counts")) is not dict
        or any(
            type(name) is not str
            or type(count) is not int
            or count < 1
            or count > _MAX_EVENTS
            for name, count in ledger.get("event_counts", {}).items()
        )
        or type(ledger.get("reasons")) is not list
        or not _json_equal(ledger, expected_ledger)
    ):
        _fail("closure_outer_projection_invalid")

    inner_reasons = tuple(denominator_summary.inner_closure_reasons)
    telemetry_reasons = _ordered_reasons(inner_reasons, ledger_reasons)
    expected_closure = {
        "claim_allowed": not telemetry_reasons,
        "claim_scope": "derived_observed_provider_responses_in_this_run_only",
        "denominator_algorithm": plan.denominator.get("denominator_algorithm"),
        "exact_response_count_preregistered": False,
        "derived_after_run": True,
        "observed_response_count": denominator_summary.observed_response_count,
        "reasons": list(telemetry_reasons),
    }
    closure = value.get("closure")
    if (
        type(closure) is not dict
        or set(closure) != _INNER_CLOSURE_FIELDS
        or type(closure.get("claim_allowed")) is not bool
        or type(closure.get("exact_response_count_preregistered")) is not bool
        or type(closure.get("derived_after_run")) is not bool
        or type(closure.get("observed_response_count")) is not int
        or type(closure.get("reasons")) is not list
        or not _json_equal(closure, expected_closure)
    ):
        _fail("closure_outer_projection_invalid")
    return ledger_reasons, telemetry_reasons


def _validate_budget_and_usage(
    *,
    denominator_summary: Any,
    analysis: _EventAnalysis,
    plan: _Plan,
) -> tuple[bool, int | None, int | None, str | None, tuple[str, ...]]:
    accepted_usage_complete = bool(denominator_summary.responses)
    response_totals = [0] * 7
    for response in denominator_summary.responses:
        for ordinal, value in enumerate((
            response.requests,
            response.input_tokens,
            response.output_tokens,
            response.total_tokens,
            response.cached_input_tokens,
            response.cache_write_tokens,
            response.reasoning_tokens,
        )):
            if value is not None:
                if type(value) is not int or value < 0:
                    _fail("closure_budget_invalid")
                if value > _MAX_SAFE_INTEGER:
                    _fail("closure_budget_invalid")
                response_totals[ordinal] += value
        accepted_usage_complete = accepted_usage_complete and (
            response.usage_complete
            and response.input_tokens is not None
            and response.output_tokens is not None
            and response.total_tokens is not None
        )
    if any(total > _MAX_SAFE_INTEGER for total in response_totals):
        _fail("closure_budget_invalid")
    if (
        accepted_usage_complete
        is not denominator_summary.accepted_record_usage_complete
    ):
        _fail("closure_denominator_invalid")
    usage_complete = (
        accepted_usage_complete
        and analysis.record_usage_index_coverage_complete
        and denominator_summary.accepted_response_count
        == denominator_summary.attempt_count
        == analysis.network_attempt_count
        and all(
            case.sdk_raw_response_reconciliation == "matched"
            and case.sdk_usage_request_reconciliation == "matched"
            for case in denominator_summary.cases
        )
    )

    input_total = denominator_summary.accepted_record_input_token_total
    output_total = denominator_summary.accepted_record_output_token_total
    total_total = denominator_summary.accepted_record_total_token_total
    if accepted_usage_complete:
        if (
            type(input_total) is not int
            or type(output_total) is not int
            or type(total_total) is not int
            or input_total < 0
            or output_total < 0
            or total_total != input_total + output_total
            or max(input_total, output_total, total_total) > _MAX_SAFE_INTEGER
        ):
            _fail("closure_budget_invalid")
    else:
        if any(value is not None for value in (input_total, output_total, total_total)):
            _fail("closure_budget_invalid")
        input_total = output_total = None

    model_inputs: list[int] = []
    model_outputs: list[int] = []
    model_cached: list[int] = []
    for run in analysis.runs:
        for event in run.model_call_events:
            for field in ("input_tokens", "output_tokens", "cached_tokens"):
                value = event.payload[field]
                if value is not None:
                    if type(value) is not int or value > _MAX_SAFE_INTEGER:
                        _fail("closure_budget_invalid")
                    if field == "input_tokens":
                        model_inputs.append(value)
                    elif field == "output_tokens":
                        model_outputs.append(value)
                    else:
                        model_cached.append(value)
        usage = run.usage_event.payload
        for field in (
            "request_count",
            "input_unit_count",
            "output_unit_count",
            "total_unit_count",
            "cached_input_unit_count",
            "response_detail_count",
            "model_call_rows_recorded",
        ):
            value = usage[field]
            if value is not None and (
                type(value) is not int or value > _MAX_SAFE_INTEGER
            ):
                _fail("closure_budget_invalid")
    if any(total > _MAX_SAFE_INTEGER for total in (
        sum(model_inputs), sum(model_outputs),
        sum(model_inputs) + sum(model_outputs), sum(model_cached),
    )):
        _fail("closure_budget_invalid")
    if usage_complete and (
        len(model_inputs) != analysis.model_call_count
        or len(model_outputs) != analysis.model_call_count
        or sum(model_inputs) != input_total
        or sum(model_outputs) != output_total
    ):
        _fail("closure_event_projection_invalid")

    extra_reasons: list[str] = []
    if not usage_complete:
        if denominator_summary.responses and not accepted_usage_complete:
            extra_reasons.append("record_usage_incomplete")
        if (
            not analysis.record_usage_index_coverage_complete
            or denominator_summary.accepted_response_count
            != denominator_summary.attempt_count
            or denominator_summary.attempt_count != analysis.network_attempt_count
            or any(
                case.sdk_raw_response_reconciliation != "matched"
                or case.sdk_usage_request_reconciliation != "matched"
                for case in denominator_summary.cases
            )
        ):
            extra_reasons.append("record_usage_index_coverage_mismatch")
        return False, None, None, None, tuple(extra_reasons)
    if input_total is None or output_total is None:  # defensive summary invariant
        _fail("closure_budget_invalid")
    cost = _exact_cost(
        input_total, output_total, plan.input_price, plan.output_price
    )
    cost_text = _observed_money(cost)
    if input_total > plan.input_token_limit_total:
        extra_reasons.append("observed_input_token_cap_exceeded")
    if output_total > plan.output_token_limit_total or any(
        response.output_tokens is not None
        and response.output_tokens > plan.output_token_limit_per_request
        for response in denominator_summary.responses
    ):
        extra_reasons.append("observed_output_token_cap_exceeded")
    if cost > plan.cost_stop:
        extra_reasons.append("observed_cost_stop_exceeded")
    return True, input_total, output_total, cost_text, tuple(extra_reasons)


def _validate_timeline(
    analysis: _EventAnalysis,
    *,
    postrun_completed_at_utc: str,
    authorization_consumed_at_utc: str,
    task_released_at_utc: str | None,
    provider_key_loaded_at_utc: str | None,
) -> None:
    try:
        completed = parse_utc_timestamp(postrun_completed_at_utc)
        consumed = parse_utc_timestamp(authorization_consumed_at_utc)
        released = (
            None
            if task_released_at_utc is None
            else parse_utc_timestamp(task_released_at_utc)
        )
        key_loaded = (
            None
            if provider_key_loaded_at_utc is None
            else parse_utc_timestamp(provider_key_loaded_at_utc)
        )
        if released is not None and (not consumed < released or completed < released):
            _fail("closure_timeline_invalid")
        if key_loaded is not None and (
            not consumed < key_loaded or completed < key_loaded
        ):
            _fail("closure_timeline_invalid")

        ordered_sends: list[tuple[int, Any]] = []
        for run in analysis.runs:
            event_times = [
                parse_utc_timestamp(event.row["occurred_at_utc"])
                for event in run.events
            ]
            if any(right < left for left, right in zip(event_times, event_times[1:])):
                _fail("closure_timeline_invalid")
            created = parse_utc_timestamp(run.run["created_at_utc"])
            updated = parse_utc_timestamp(run.run["updated_at_utc"])
            if created != event_times[0] or updated != event_times[-1]:
                _fail("closure_timeline_invalid")
            if any(completed < timestamp for timestamp in event_times):
                _fail("closure_timeline_invalid")
            for group in run.attempts:
                started = parse_utc_timestamp(group.started.row["occurred_at_utc"])
                terminal = parse_utc_timestamp(group.terminal.row["occurred_at_utc"])
                if terminal < started:
                    _fail("closure_timeline_invalid")
                if group.sent is not None:
                    sent = parse_utc_timestamp(group.sent.row["occurred_at_utc"])
                    if sent < started or terminal < sent or not consumed < sent:
                        _fail("closure_timeline_invalid")
                    if (
                        released is None
                        or key_loaded is None
                        or sent < released
                        or sent < key_loaded
                    ):
                        _fail("closure_timeline_invalid")
                    ordered_sends.append(
                        (group.sent.payload["network_call_index"], sent)
                    )
        if any(
            right[1] < left[1]
            for left, right in zip(ordered_sends, ordered_sends[1:])
        ):
            _fail("closure_timeline_invalid")
    except ExternalClosurePrimitiveError as error:
        if error.code == "closure_timeline_invalid":
            raise
        _fail("closure_timeline_invalid")
    except Exception:
        _fail("closure_timeline_invalid")


def _validate_model_call_timeline(
    analysis: _EventAnalysis,
    model_call_rows: Sequence[Mapping[str, Any]],
    *,
    postrun_completed_at_utc: str,
) -> None:
    try:
        completed = parse_utc_timestamp(postrun_completed_at_utc)
        rows = {row["model_call_id"]: row for row in model_call_rows}
        for run in analysis.runs:
            run_started = parse_utc_timestamp(run.events[0].row["occurred_at_utc"])
            for event in run.model_call_events:
                row = rows[event.payload["model_call_id"]]
                started = parse_utc_timestamp(row["started_at_utc"])
                recorded = parse_utc_timestamp(event.row["occurred_at_utc"])
                if started < run_started or recorded < started or completed < started:
                    _fail("closure_timeline_invalid")
    except ExternalClosurePrimitiveError as error:
        if error.code == "closure_timeline_invalid":
            raise
        _fail("closure_timeline_invalid")
    except Exception:
        _fail("closure_timeline_invalid")


def verify_artifact_content_semantics(
    telemetry: Mapping[str, Any],
    audit_index: Mapping[str, Any],
    *,
    run_rows: Sequence[Mapping[str, Any]],
    event_rows: Sequence[Mapping[str, Any]],
    model_call_rows: Sequence[Mapping[str, Any]],
    persisted_evidence_context: object,
    envelope_id: str,
    campaign_id: str,
    expected_runtime_plan: Mapping[str, Any],
    authorization_grant_sha256: str,
    execution_input_order_commitment_sha256: str,
    api_origin: str,
    api_path: str,
    model_id: str,
    postrun_completed_at_utc: str,
    authorization_consumed_at_utc: str,
    task_released_at_utc: str | None,
    provider_key_loaded_at_utc: str | None,
    tool_call_count: int = 0,
    tool_attempt_count: int = 0,
    approval_decision_count: int = 0,
) -> VerifiedArtifactContentSemantics:
    """Recompute the complete semantic projection of persisted artifact data.

    No value from ``telemetry.closure``, ``ledger_reconciliation`` or the audit
    index is trusted.  The persisted-only denominator validator is invoked for
    the denominator itself; this layer then binds that result to independently
    extracted audit rows, bridge commitments, exact pricing, and frozen
    external-plan projections.
    """

    events = _snapshot_rows(
        event_rows, maximum=_MAX_EVENTS, code="closure_audit_chain_invalid"
    )
    raw_event_runs, expected_chains = _validate_raw_chains(events)
    index_snapshot = _snapshot_mapping(
        audit_index, "closure_audit_run_set_invalid"
    )
    _validate_reported_chains(
        index_snapshot, expected_chains=expected_chains
    )
    plan = _validate_plan(
        expected_runtime_plan, envelope_id=envelope_id, campaign_id=campaign_id
    )
    runs = _snapshot_rows(
        run_rows, maximum=_MAX_RUNS, code="closure_audit_run_set_invalid"
    )
    index, ordered_runs = _validate_index_and_run_set(
        index_snapshot, runs, plan=plan
    )
    decoded = _decode_event_runs(
        raw_event_runs, ordered_run_rows=ordered_runs
    )
    _validate_event_skeleton(decoded)
    model_calls = _snapshot_rows(
        model_call_rows,
        maximum=_MAX_ATTEMPTS,
        code="closure_event_projection_invalid",
    )
    if any(
        type(value) is not int or value != 0
        for value in (
            tool_call_count,
            tool_attempt_count,
            approval_decision_count,
        )
    ):
        _fail("closure_event_projection_invalid")
    _prevalidate_bridge_commitments(index, decoded, plan=plan)
    _prevalidate_model_call_rows(
        model_calls,
        ordered_run_rows=ordered_runs,
        plan=plan,
        model_id=model_id,
    )
    _prevalidate_event_projections(
        decoded,
        ordered_runs=ordered_runs,
        model_calls=model_calls,
        plan=plan,
        campaign_id=campaign_id,
        authorization_grant_sha256=authorization_grant_sha256,
        api_origin=api_origin,
        api_path=api_path,
        model_id=model_id,
        persisted_evidence_context=persisted_evidence_context,
    )
    telemetry_snapshot = _snapshot_mapping(
        telemetry, "closure_outer_projection_invalid"
    )
    denominator_artifact = telemetry_snapshot.get("runtime_denominator")
    if type(denominator_artifact) is not dict:
        _fail("closure_denominator_invalid")
    try:
        denominator_summary = summarize_persisted_live_runtime_denominator_artifact(
            denominator_artifact,
            context=persisted_evidence_context,  # type: ignore[arg-type]
        )
    except Exception:
        _fail("closure_denominator_invalid")
    analysis = _validate_events(
        index=index,
        ordered_run_rows=ordered_runs,
        decoded_event_runs=decoded,
        model_call_rows=model_calls,
        denominator_summary=denominator_summary,
        denominator_artifact=denominator_artifact,
        plan=plan,
        campaign_id=campaign_id,
        authorization_grant_sha256=authorization_grant_sha256,
        execution_input_order_commitment_sha256=(
            execution_input_order_commitment_sha256
        ),
        api_origin=api_origin,
        api_path=api_path,
        model_id=model_id,
    )
    ledger_reasons, telemetry_reasons = _validate_outer_telemetry(
        telemetry_snapshot,
        plan=plan,
        denominator_summary=denominator_summary,
        analysis=analysis,
    )
    (
        usage_complete,
        observed_input_tokens,
        observed_output_tokens,
        observed_cost_cny,
        budget_reasons,
    ) = _validate_budget_and_usage(
        denominator_summary=denominator_summary,
        analysis=analysis,
        plan=plan,
    )
    _validate_timeline(
        analysis,
        postrun_completed_at_utc=postrun_completed_at_utc,
        authorization_consumed_at_utc=authorization_consumed_at_utc,
        task_released_at_utc=task_released_at_utc,
        provider_key_loaded_at_utc=provider_key_loaded_at_utc,
    )
    _validate_model_call_timeline(
        analysis,
        model_calls,
        postrun_completed_at_utc=postrun_completed_at_utc,
    )

    observed_run_ids = plan.run_ids[: len(ordered_runs)]
    observed_run_set_commitment = _domain_hash(
        _AUDIT_RUN_SET_DOMAIN,
        b"observed",
        canonical_json_bytes(list(observed_run_ids)),
    )
    chain_head_projection = [
        {
            "run_id": run_id,
            "final_chain_head_sha256": index["runs"][ordinal][
                "chain_verification"
            ]["chain_head"],
        }
        for ordinal, run_id in enumerate(observed_run_ids)
    ]
    ordered_chain_heads_commitment = _domain_hash(
        _AUDIT_RUN_SET_DOMAIN,
        b"final_chain_heads",
        canonical_json_bytes(chain_head_projection),
    )
    semantic_reasons = _ordered_reasons(
        denominator_summary.inner_closure_reasons,
        ledger_reasons,
        budget_reasons,
    )
    last_error = ordered_runs[-1]["terminal_error_code"] if ordered_runs else None
    return VerifiedArtifactContentSemantics._create(
        _SUMMARY_TOKEN,
        observed_run_count=len(ordered_runs),
        full_plan_observed=len(ordered_runs) == len(plan.case_ids),
        observed_audit_run_set_commitment_sha256=observed_run_set_commitment,
        ordered_final_chain_heads_commitment_sha256=(
            ordered_chain_heads_commitment
        ),
        planned_case_count=denominator_summary.planned_case_count,
        attempt_count=denominator_summary.attempt_count,
        accepted_response_count=denominator_summary.accepted_response_count,
        network_attempt_count_observed=analysis.network_attempt_count,
        model_request_count_observed=analysis.model_request_count,
        model_call_count=analysis.model_call_count,
        usage_complete=usage_complete,
        observed_input_tokens=observed_input_tokens,
        observed_output_tokens=observed_output_tokens,
        observed_cost_cny=observed_cost_cny,
        all_chains_valid=True,
        ledger_failure_observed=analysis.ledger_failure_observed,
        inner_closure_claim_allowed=denominator_summary.inner_closure_claim_allowed,
        inner_closure_reasons=tuple(denominator_summary.inner_closure_reasons),
        ledger_reconciliation_reasons=ledger_reasons,
        telemetry_closure_claim_allowed=not telemetry_reasons,
        telemetry_closure_reasons=telemetry_reasons,
        semantic_closure_claim_allowed=not semantic_reasons,
        semantic_closure_reasons=semantic_reasons,
        last_observed_run_terminal_error_code=last_error,
    )


__all__ = [
    "VerifiedArtifactContentSemantics",
    "verify_artifact_content_semantics",
]
