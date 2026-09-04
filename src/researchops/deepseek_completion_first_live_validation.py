from __future__ import annotations

import asyncio
import hashlib
import importlib.metadata
import json
import logging
import math
import os
import re
import subprocess
import time
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Mapping
from urllib.parse import urlsplit

from .audit import (
    COMPLETION_TELEMETRY_EVENT_SCHEMA_VERSION,
    COMPLETION_TELEMETRY_UNMAPPED_EVENT,
    AuditLedger,
    sha256_json,
)
from .completion_telemetry_ledger import LedgerCompletionTelemetrySession
from .phase6_runner import _finalize_runtime_completion_telemetry
from researchops_completion_telemetry.capture import (
    CompletionCaptureError,
    CompletionTelemetryCollector,
    RuntimeDenominatorTracker,
    VerifiedRuntimeDenominatorPlanBinding,
    verify_runtime_denominator_plan,
)
from researchops_completion_telemetry.sanitization import (
    build_completion_record,
    validate_runtime_denominator_artifact,
)
import researchops_completion_telemetry.surface_mapping as surface_mapping
from researchops_completion_telemetry.surface_mapping import (
    VerifiedRuntimeCompletionBinding,
    _create_first_live_validation_binding,
    load_and_select_surface_mapping,
)


CONTRACT_RELATIVE_PATH = Path(
    "evals/provider_completion_first_live_validation_v1/"
    "deepseek_responses_adapter_validation_contract_v1.json"
)
CONTRACT_BYTES = 9_991
CONTRACT_FILE_SHA256 = (
    "477addbc41987d11c6ac7ede3fa6f94d74322efe09c541ea20f3a7937eb96496"
)
CONTRACT_COMMITMENT_SHA256 = (
    "ddff10f30031faf77d6417dd695dd61dae4c6a45334efae7388ab4f2adc4a5bc"
)
IMPLEMENTATION_RELATIVE_PATH = Path(
    "evals/provider_completion_first_live_validation_v1/"
    "deepseek_responses_adapter_validation_implementation_v2.json"
)
IMPLEMENTATION_BYTES = 7_374
IMPLEMENTATION_FILE_SHA256 = (
    "46dfe459f8c5d610c504af68f44b0369392c8c61c8479b82239a30d4f5a776eb"
)
IMPLEMENTATION_COMMITMENT_SHA256 = (
    "d28a76b6a2268e71f78cd95e0ee99c7295b0636fe56495319575ef10b7ca4a5e"
)
SOURCE_INTEGRITY_PLAN_RELATIVE_PATH = Path(
    "evals/phase6_deepseek_depth60_plan_v5.json"
)
SOURCE_INTEGRITY_PLAN_ID = "phase6-deepseek-depth60-v5"
VALIDATION_ID = "deepseek-responses-adapter-first-live-validation-v1"
VALIDATION_CASE_ID = "DEEPSEEK-FIRST-LIVE-VALIDATION-001"
CONSUMPTION_SCHEMA_VERSION = "deepseek-first-live-consumption/1.0"
TERMINAL_SCHEMA_VERSION = "deepseek-first-live-terminal/1.0"
EVIDENCE_SCHEMA_VERSION = "deepseek-first-live-evidence/1.0"
MANIFEST_SCHEMA_VERSION = "deepseek-first-live-manifest/1.0"
_REQUEST_TIMEOUT_SECONDS = 120.0
_REQUEST_PHASE_TIMEOUT_SECONDS = 300.0
_WHOLE_PROCESS_TIMEOUT_SECONDS = 330.0
_TERMINALIZATION_RESERVE_SECONDS = 30.0

_AUTHORIZATION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_GIT_SHA = re.compile(r"^[0-9a-f]{40}$")
_SAFE_ERROR = re.compile(r"^[a-z][a-z0-9_]{0,95}$")
_PRICING_SOURCE_URL = "https://api-docs.deepseek.com/zh-cn/quick_start/pricing/"
_AUTHORITY_TOKEN = object()
_NETWORK_LOGGERS = ("openai", "httpx", "httpcore", "agents")
_FORBIDDEN_OPENAI_ENVIRONMENT = (
    "OPENAI_ORG_ID",
    "OPENAI_PROJECT_ID",
    "OPENAI_ADMIN_KEY",
    "OPENAI_WEBHOOK_SECRET",
    "OPENAI_CUSTOM_HEADERS",
    "OPENAI_LOG",
)
_FORBIDDEN_ARTIFACT_PATTERNS = (
    re.compile(rb"sk-[A-Za-z0-9_-]{8,}"),
    re.compile(rb"(?i)authorization\s*:\s*(?:bearer|basic)\s+"),
    re.compile(rb"(?i)[A-Z]:\\(?:Users|Windows|ProgramData|Temp)\\"),
    re.compile(rb"/(?:Users|home|tmp|var/tmp)/"),
    re.compile(rb"Traceback \(most recent call last\)"),
    re.compile(rb"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"),
)
_CONSUMPTION_FIELDS = frozenset(
    {
        "schema_version",
        "status",
        "contract_id",
        "contract_commitment_sha256",
        "implementation_commitment_sha256",
        "source_integrity_plan_id",
        "source_integrity_commitment_sha256",
        "execution_commit",
        "authorization_id_sha256",
        "authorization_binding_sha256",
        "authorization_expires_at_utc",
        "consumed_at_utc",
        "pricing_snapshot_date",
        "pricing_source_url",
        "input_price_per_million_cny",
        "output_price_per_million_cny",
        "consume_before_key_load",
        "provider_key_loaded_at_consumption",
        "network_attempts_at_consumption",
        "model_requests_at_consumption",
        "authorizes_retry",
        "authorizes_resume",
        "authorizes_evaluation",
        "authorizes_status_closure",
    }
)
_TERMINAL_FIELDS = frozenset(
    {
        "schema_version",
        "status",
        "error_code",
        "outcome_unknown",
        "contract_id",
        "contract_commitment_sha256",
        "implementation_commitment_sha256",
        "source_integrity_plan_id",
        "source_integrity_commitment_sha256",
        "execution_commit",
        "authorization_id_sha256",
        "authorization_binding_sha256",
        "authorization_expires_at_utc",
        "consumption_receipt_sha256",
        "started_at_utc",
        "completed_at_utc",
        "network_attempts",
        "network_attempt_limit",
        "network_calls",
        "network_call_observation_complete",
        "model_requests",
        "model_request_limit",
        "input_tokens",
        "input_token_limit",
        "output_tokens",
        "output_token_limit",
        "usage_complete",
        "local_observed_usage_cost_cny",
        "local_observed_usage_cost_stop_cny",
        "strict_provider_billing_hard_cap",
        "actual_provider_billed_cost_cny",
        "manifest_sha256",
        "manifest_complete",
        "partial_artifacts",
        "ledger_run_status",
        "provider_key_loaded",
        "provider_key_persisted",
        "raw_response_body_persisted",
        "raw_response_cleanup_complete",
        "message_content_persisted",
        "input_content_repeated_in_receipt",
        "exception_text_persisted",
        "closure_claim_allowed",
        "automatic_registry_promotion_allowed",
        "authorizes_retry",
        "authorizes_resume",
        "authorizes_evaluation",
        "authorizes_provider_registration",
        "authorizes_model_quality_claim",
    }
)
_SUCCESS_EVIDENCE_FIELDS = frozenset(
    {
        "schema_version",
        "status",
        "contract_id",
        "contract_commitment_sha256",
        "implementation_commitment_sha256",
        "runtime_plan",
        "runtime_denominator",
        "ledger_reconciliation",
        "validation_integrity_gate_passed",
        "observed_states_in_order",
        "observed_signal_sources_in_order",
        "observed_completion_shapes_in_order",
        "observed_input_tokens",
        "observed_output_tokens",
        "local_observed_cost_cny",
        "status_defect_closure_allowed",
        "closure_claim_allowed",
        "automatic_registry_promotion_allowed",
        "raw_response_body_persisted",
        "message_content_persisted",
        "provider_key_persisted",
    }
)
_FAILURE_EVIDENCE_FIELDS = frozenset(
    {
        "schema_version",
        "status",
        "error_code",
        "outcome_unknown",
        "contract_id",
        "contract_commitment_sha256",
        "implementation_commitment_sha256",
        "runtime_plan",
        "runtime_denominator",
        "ledger_reconciliation",
        "validation_integrity_gate_passed",
        "status_defect_closure_allowed",
        "closure_claim_allowed",
        "automatic_registry_promotion_allowed",
        "raw_response_body_persisted",
        "message_content_persisted",
        "provider_key_persisted",
    }
)
_MANIFEST_FIELDS = frozenset(
    {
        "schema_version",
        "status",
        "contract_commitment_sha256",
        "implementation_commitment_sha256",
        "files",
        "raw_response_body_persisted",
        "message_content_persisted",
        "api_key_persisted",
    }
)
_AUDIT_INDEX_FIELDS = frozenset({"runs"})
_AUDIT_RUN_ENTRY_FIELDS = frozenset(
    {
        "case_id",
        "run_id",
        "chain_verification",
        "completion_telemetry_event_commitment",
    }
)
_ARTIFACT_FILENAMES = frozenset(
    {
        "consumption.json",
        "terminal.json",
        "audit.sqlite3",
        "audit_index.json",
        "runtime_denominator.json",
        "completion_telemetry.json",
        "manifest.json",
    }
)


class DeepSeekFirstLiveValidationError(RuntimeError):
    """Stable error that never includes Provider or filesystem payloads."""

    def __init__(
        self,
        code: str,
        message: str = "DeepSeek first-live validation failed.",
        *,
        not_run: bool = False,
        outcome_unknown: bool = False,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.not_run = not_run
        self.outcome_unknown = outcome_unknown

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": "not_run" if self.not_run else "failed",
            "error_code": self.code,
            "outcome_unknown": self.outcome_unknown,
        }


def _error(
    code: str,
    *,
    not_run: bool = False,
    outcome_unknown: bool = False,
) -> DeepSeekFirstLiveValidationError:
    return DeepSeekFirstLiveValidationError(
        code,
        not_run=not_run,
        outcome_unknown=outcome_unknown,
    )


def _strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _error("deepseek_first_live_duplicate_json_key", not_run=True)
        result[key] = value
    return result


def _reject_nonfinite(value: str) -> object:
    del value
    raise _error("deepseek_first_live_nonfinite_json", not_run=True)


def _decode_json_object(payload: bytes) -> dict[str, Any]:
    try:
        value = json.loads(
            payload.decode("utf-8"),
            object_pairs_hook=_strict_object,
            parse_constant=_reject_nonfinite,
        )
    except DeepSeekFirstLiveValidationError:
        raise
    except (UnicodeError, ValueError, json.JSONDecodeError):
        raise _error("deepseek_first_live_contract_json_invalid", not_run=True) from None
    if not isinstance(value, dict):
        raise _error("deepseek_first_live_contract_shape_invalid", not_run=True)
    return value


def _canonical_bytes(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError):
        raise _error("deepseek_first_live_canonical_json_invalid") from None


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _require_exact_fields(
    value: object,
    expected: frozenset[str],
    code: str,
) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or set(value) != expected:
        raise _error(code)
    return value


def _safe_fixed_file(root: Path, relative: str | Path) -> Path:
    raw = PurePosixPath(str(relative).replace("\\", "/"))
    if raw.is_absolute() or any(part in {"", ".", ".."} for part in raw.parts):
        raise _error("deepseek_first_live_bound_path_invalid", not_run=True)
    lexical = root.joinpath(*raw.parts)
    current = root
    for part in raw.parts:
        current = current / part
        if _path_is_link_like(current):
            raise _error("deepseek_first_live_bound_path_invalid", not_run=True)
    try:
        path = lexical.resolve(strict=True)
    except OSError:
        raise _error("deepseek_first_live_bound_file_missing", not_run=True) from None
    if not path.is_relative_to(root) or not path.is_file():
        raise _error("deepseek_first_live_bound_path_invalid", not_run=True)
    return path


def _contract_commitment(value: Mapping[str, Any]) -> str:
    body = json.loads(json.dumps(value, ensure_ascii=False))
    commitment = body.get("contract_commitment")
    if not isinstance(commitment, dict):
        raise _error("deepseek_first_live_contract_shape_invalid", not_run=True)
    domain = commitment.get("domain")
    if domain != "researchops-provider-completion-first-live-validation-v1":
        raise _error("deepseek_first_live_contract_identity_invalid", not_run=True)
    commitment.pop("sha256", None)
    return _sha256(domain.encode("utf-8") + b"\0" + _canonical_bytes(body))


def validate_deepseek_first_live_contract(
    project_root: str | Path,
) -> dict[str, Any]:
    """Validate the confirmed design contract and every referenced local byte."""

    root = Path(project_root).resolve()
    contract_path = _safe_fixed_file(root, CONTRACT_RELATIVE_PATH)
    payload = contract_path.read_bytes()
    if len(payload) != CONTRACT_BYTES or _sha256(payload) != CONTRACT_FILE_SHA256:
        raise _error("deepseek_first_live_contract_file_drift", not_run=True)
    contract = _decode_json_object(payload)
    commitment = contract.get("contract_commitment")
    if (
        contract.get("schema_version")
        != "provider-completion-first-live-validation/1.0"
        or contract.get("contract_id") != VALIDATION_ID
        or contract.get("status") != "design_only_offline_not_executable"
        or not isinstance(commitment, dict)
        or commitment.get("sha256") != CONTRACT_COMMITMENT_SHA256
        or _contract_commitment(contract) != CONTRACT_COMMITMENT_SHA256
    ):
        raise _error("deepseek_first_live_contract_identity_invalid", not_run=True)
    implementation = contract.get("implementation_state")
    if implementation != {
        "contract_only": True,
        "cli_implemented": False,
        "validation_only_authority_implemented": False,
        "single_use_receipts_implemented": False,
        "online_execution_authorized": False,
        "network_calls": 0,
        "model_calls": 0,
        "provider_key_loaded": False,
    }:
        raise _error("deepseek_first_live_contract_boundary_invalid", not_run=True)
    provider = contract.get("provider_binding")
    limits = contract.get("hard_limits")
    frozen = contract.get("frozen_inputs")
    if (
        not isinstance(provider, dict)
        or provider.get("provider_id") != "deepseek"
        or provider.get("model_id") != "deepseek-v4-flash"
        or provider.get("api_origin") != "https://api.deepseek.com"
        or provider.get("api_surface") != "responses"
        or provider.get("transport_id") != "openai_compatible_responses"
        or provider.get("adapter_version") != "deepseek-responses-adapter/1.0"
        or provider.get("stream") is not False
        or provider.get("tools") is not False
        or provider.get("fallback") is not False
        or not isinstance(limits, dict)
        or limits.get("network_attempts_max") != 2
        or limits.get("network_calls_max") != 2
        or limits.get("model_requests_max") != 2
        or limits.get("concurrency") != 1
        or limits.get("agents_sdk_retries") != 0
        or limits.get("http_client_retries") != 0
        or limits.get("resume") is not False
        or limits.get("fallback") is not False
        or limits.get("tools") != 0
        or limits.get("output_token_caps_in_order") != [256, 16]
        or limits.get("output_token_cap_total") != 272
        or limits.get("whole_process_wall_timeout_seconds") != 330
        or not isinstance(frozen, dict)
        or frozen.get("scenario_count") != 2
        or frozen.get("scenario_order_locked") is not True
        or frozen.get("prompt_or_scenario_adjustment_after_lock_allowed") is not False
    ):
        raise _error("deepseek_first_live_contract_boundary_invalid", not_run=True)
    scenarios = frozen.get("scenarios")
    if not isinstance(scenarios, list) or len(scenarios) != 2:
        raise _error("deepseek_first_live_contract_boundary_invalid", not_run=True)
    scenario_bytes = [
        len(str(item.get("input", "")).encode("utf-8"))
        if isinstance(item, dict)
        else 2**31
        for item in scenarios
    ]
    if (
        any(size > limits.get("input_utf8_bytes_per_request_max", -1) for size in scenario_bytes)
        or sum(scenario_bytes) > limits.get("input_utf8_bytes_total_max", -1)
        or [item.get("max_output_tokens") for item in scenarios]
        != limits["output_token_caps_in_order"]
        or [item.get("expected_normalized_completion_state") for item in scenarios]
        != ["completed", "incomplete_length"]
    ):
        raise _error("deepseek_first_live_scenario_contract_invalid", not_run=True)
    bindings = contract.get("telemetry_bindings")
    if not isinstance(bindings, dict):
        raise _error("deepseek_first_live_contract_binding_invalid", not_run=True)
    for item in bindings.values():
        if not isinstance(item, dict) or "relative_path" not in item:
            continue
        path = _safe_fixed_file(root, item["relative_path"])
        raw = path.read_bytes()
        if (
            type(item.get("bytes")) is not int
            or len(raw) != item["bytes"]
            or not isinstance(item.get("sha256"), str)
            or _sha256(raw) != item["sha256"]
        ):
            raise _error("deepseek_first_live_contract_binding_invalid", not_run=True)
    return contract


def validate_deepseek_first_live_implementation(
    project_root: str | Path,
) -> dict[str, Any]:
    root = Path(project_root).resolve()
    validate_deepseek_first_live_contract(root)
    path = _safe_fixed_file(root, IMPLEMENTATION_RELATIVE_PATH)
    payload = path.read_bytes()
    if len(payload) != IMPLEMENTATION_BYTES or _sha256(payload) != IMPLEMENTATION_FILE_SHA256:
        raise _error("deepseek_first_live_implementation_file_drift", not_run=True)
    value = _decode_json_object(payload)
    commitment = value.get("implementation_commitment")
    if not isinstance(commitment, dict):
        raise _error("deepseek_first_live_implementation_invalid", not_run=True)
    body = json.loads(json.dumps(value, ensure_ascii=False))
    body_commitment = body["implementation_commitment"]
    domain = body_commitment.get("domain")
    body_commitment.pop("sha256", None)
    computed = _sha256(domain.encode("utf-8") + b"\0" + _canonical_bytes(body)) if isinstance(domain, str) else None
    predecessor = value.get("predecessor_design")
    activation = value.get("activation_gate")
    claims = value.get("claim_boundary")
    if (
        value.get("schema_version")
        != "provider-completion-first-live-implementation/2.0"
        or value.get("implementation_id")
        != "deepseek-responses-adapter-first-live-implementation-v2"
        or value.get("status")
        != "offline_implemented_not_run_requires_fresh_authorization"
        or commitment.get("sha256") != IMPLEMENTATION_COMMITMENT_SHA256
        or computed != IMPLEMENTATION_COMMITMENT_SHA256
        or not isinstance(predecessor, dict)
        or predecessor.get("contract_commitment_sha256")
        != CONTRACT_COMMITMENT_SHA256
        or predecessor.get("immutable") is not True
        or not isinstance(activation, dict)
        or activation.get("source_integrity_plan_id")
        != SOURCE_INTEGRITY_PLAN_ID
        or activation.get("source_integrity_plan_relative_path")
        != SOURCE_INTEGRITY_PLAN_RELATIVE_PATH.as_posix()
        or not isinstance(claims, dict)
        or claims.get("online_execution_authorized") is not False
        or claims.get("status_defect_closure_allowed") is not False
        or claims.get("automatic_registry_promotion_allowed") is not False
    ):
        raise _error("deepseek_first_live_implementation_invalid", not_run=True)
    return value


def deepseek_first_live_validation_status(
    project_root: str | Path,
) -> dict[str, Any]:
    contract = validate_deepseek_first_live_contract(project_root)
    validate_deepseek_first_live_implementation(project_root)
    root = Path(project_root).resolve()
    source_successor_present = (root / SOURCE_INTEGRITY_PLAN_RELATIVE_PATH).is_file()
    source_commitment: str | None = None
    if source_successor_present:
        try:
            from .phase6_depth60 import validate_phase6_depth60_plan

            source_result = validate_phase6_depth60_plan(
                root, SOURCE_INTEGRITY_PLAN_RELATIVE_PATH
            )
        except Exception:
            raise _error("deepseek_first_live_source_integrity_invalid", not_run=True) from None
        if (
            source_result.get("status") != "valid"
            or source_result.get("plan_id") != SOURCE_INTEGRITY_PLAN_ID
            or source_result.get("online_execution_authorized") is not False
            or source_result.get("network_calls") != 0
            or source_result.get("model_calls") != 0
        ):
            raise _error("deepseek_first_live_source_integrity_invalid", not_run=True)
        source_commitment = source_result["plan_commitment_sha256"]
    return {
        "status": (
            "offline_implemented_requires_fresh_authorization"
            if source_successor_present
            else "offline_implemented_source_successor_missing"
        ),
        "contract_id": VALIDATION_ID,
        "contract_commitment_sha256": CONTRACT_COMMITMENT_SHA256,
        "scenario_count": contract["frozen_inputs"]["scenario_count"],
        "design_contract_status": contract["status"],
        "implementation_commitment_sha256": (
            IMPLEMENTATION_COMMITMENT_SHA256
        ),
        "cli_implemented": True,
        "validation_only_authority_implemented": True,
        "single_use_receipts_implemented": True,
        "source_integrity_successor_present": source_successor_present,
        "source_integrity_plan_id": (
            SOURCE_INTEGRITY_PLAN_ID if source_successor_present else None
        ),
        "source_integrity_commitment_sha256": source_commitment,
        "online_execution_authorized": False,
        "network_calls": 0,
        "model_calls": 0,
        "provider_key_loaded": False,
    }


class _ConsumedValidationAuthorization:
    __slots__ = (
        "authorization_id_sha256",
        "authorization_binding_sha256",
        "authorization_expires_at_utc",
        "contract_commitment_sha256",
        "implementation_commitment_sha256",
        "execution_commit",
        "source_integrity_commitment_sha256",
        "input_price_per_million_cny",
        "output_price_per_million_cny",
        "_authority_token",
        "_session_claimed",
    )

    def __init__(self, *args: object, **kwargs: object) -> None:
        del args, kwargs
        raise TypeError("Consumed validation authorization is opaque")

    @classmethod
    def _create(cls, token: object, **values: Any) -> _ConsumedValidationAuthorization:
        if token is not _AUTHORITY_TOKEN:
            raise TypeError("invalid first-live authority token")
        instance = object.__new__(cls)
        for name, value in values.items():
            object.__setattr__(instance, name, value)
        object.__setattr__(instance, "_authority_token", _AUTHORITY_TOKEN)
        object.__setattr__(instance, "_session_claimed", False)
        return instance

    def assert_authority(self) -> None:
        if (
            type(self) is not _ConsumedValidationAuthorization
            or getattr(self, "_authority_token", None) is not _AUTHORITY_TOKEN
            or self.contract_commitment_sha256 != CONTRACT_COMMITMENT_SHA256
            or self.implementation_commitment_sha256
            != IMPLEMENTATION_COMMITMENT_SHA256
            or datetime.now(timezone.utc) >= self.authorization_expires_at_utc
        ):
            raise _error("deepseek_first_live_authority_invalid")

    def claim_session(self) -> None:
        self.assert_authority()
        if self._session_claimed:
            raise _error("deepseek_first_live_authority_already_claimed")
        object.__setattr__(self, "_session_claimed", True)


class _DeepSeekFirstLiveValidationLedgerSession(LedgerCompletionTelemetrySession):
    """Exact validation-only session accepted by the DeepSeek Adapter and nowhere else."""

    __slots__ = (
        "_pending_transport_handle",
        "_accepted_records",
        "_sent_attempt_indices",
        "_terminal_kinds",
        "_transport_observation_armed",
        "_transport_send_count",
        "_validation_authority",
        "_validation_run_id",
    )

    def _required_runtime_authority_scope(self) -> str:
        return "first_live_validation"

    def _is_exact_supported_session_type(self) -> bool:
        return type(self) is _DeepSeekFirstLiveValidationLedgerSession

    def __init__(
        self,
        session,
        *,
        ledger: AuditLedger,
        run_id: str,
        runtime_plan_binding: VerifiedRuntimeDenominatorPlanBinding,
        authorization: _ConsumedValidationAuthorization,
    ) -> None:
        if type(authorization) is not _ConsumedValidationAuthorization:
            raise _error("deepseek_first_live_authority_invalid")
        authorization.claim_session()
        self._validation_authority = authorization
        self._validation_run_id = run_id
        self._pending_transport_handle = None
        self._accepted_records: dict[int, dict[str, Any]] = {}
        self._sent_attempt_indices: set[int] = set()
        self._terminal_kinds: dict[int, str] = {}
        self._transport_observation_armed = False
        self._transport_send_count = 0
        super().__init__(
            session,
            ledger=ledger,
            run_id=run_id,
            runtime_plan_binding=runtime_plan_binding,
        )

    def assert_provider_telemetry_authority(self) -> None:
        self._validation_authority.assert_authority()
        super().assert_provider_telemetry_authority()

    def arm_deepseek_transport_observation(self) -> None:
        self.assert_provider_telemetry_authority()
        if self._transport_observation_armed:
            raise _error("deepseek_first_live_transport_observer_reused")
        self._transport_observation_armed = True

    async def observe_deepseek_transport_send(self, request: object) -> None:
        self.assert_provider_telemetry_authority()
        if not self._transport_observation_armed:
            raise _error("deepseek_first_live_transport_observer_not_armed")
        method = getattr(request, "method", None)
        raw_url = getattr(request, "url", None)
        parsed = urlsplit(str(raw_url)) if raw_url is not None else None
        if (
            method != "POST"
            or parsed is None
            or parsed.scheme != "https"
            or parsed.netloc != "api.deepseek.com"
            or parsed.path != "/responses"
            or parsed.query
            or parsed.fragment
        ):
            raise _error("deepseek_first_live_transport_request_invalid")
        if self._transport_send_count >= 2:
            raise _error("deepseek_first_live_network_call_cap_exceeded")
        handle = self._pending_transport_handle
        if handle is None:
            raise _error("deepseek_first_live_transport_attempt_missing")
        if handle.attempt_index in self._sent_attempt_indices:
            raise _error("deepseek_first_live_transport_retry_detected")
        next_index = self._transport_send_count
        self._ledger.append_event(
            self._validation_run_id,
            "provider_transport_request_sent",
            {
                "schema_version": "deepseek-first-live-transport-send/1.0",
                "case_id": handle.case_id,
                "attempt_index": handle.attempt_index,
                "case_attempt_index": handle.case_attempt_index,
                "network_call_index": next_index,
                "method": "POST",
                "origin": "https://api.deepseek.com",
                "path": "/responses",
            },
            actor_kind="provider_adapter",
        )
        self._sent_attempt_indices.add(handle.attempt_index)
        self._transport_send_count = next_index + 1

    def transport_observation_snapshot(self) -> tuple[int, bool]:
        self._validation_authority.assert_authority()
        return self._transport_send_count, self._transport_observation_armed

    def terminal_kinds_snapshot(self) -> tuple[str, ...]:
        self._validation_authority.assert_authority()
        return tuple(self._terminal_kinds[index] for index in sorted(self._terminal_kinds))

    def observed_request_outcome_unknown(self) -> bool:
        self._validation_authority.assert_authority()
        if not self._transport_observation_armed or self._transport_send_count == 0:
            return False
        terminal_kinds = self.terminal_kinds_snapshot()
        return (
            self._transport_send_count > len(terminal_kinds)
            or any(
                kind in {"cancelled", "outcome_unknown"}
                for kind in terminal_kinds
            )
        )

    def _finalize(
        self,
        handle,
        terminal_kind: str,
        *,
        capture=None,
        error_code: str | None = None,
    ):
        terminal = super()._finalize(
            handle,
            terminal_kind,
            capture=capture,
            error_code=error_code,
        )
        self._terminal_kinds[terminal.attempt_index] = terminal.terminal_kind
        if terminal.terminal_kind == "response_accepted":
            if terminal.response_index is None or terminal._capture is None:
                raise _error("deepseek_first_live_accepted_record_missing")
            self._accepted_records[terminal.response_index] = build_completion_record(
                terminal._capture._collector_snapshot(),
                binding=self._binding,
                response_index=terminal.response_index,
                request_index=terminal.attempt_index,
            )
        if (
            self._pending_transport_handle is not None
            and self._pending_transport_handle.attempt_index == terminal.attempt_index
        ):
            self._pending_transport_handle = None
        return terminal

    def accepted_record(self, response_index: int) -> Mapping[str, Any]:
        self._validation_authority.assert_authority()
        record = self._accepted_records.get(response_index)
        if record is None:
            raise _error("deepseek_first_live_accepted_record_missing")
        return record

    def begin_attempt(self):
        self.assert_provider_telemetry_authority()
        if self._pending_transport_handle is not None:
            raise _error("deepseek_first_live_transport_attempt_overlap")
        handle = super().begin_attempt()
        self._pending_transport_handle = handle
        return handle


def _mint_validation_tracker(
    root: Path,
    authorization: _ConsumedValidationAuthorization,
) -> tuple[RuntimeDenominatorTracker, VerifiedRuntimeDenominatorPlanBinding]:
    authorization.assert_authority()
    binding = _validation_runtime_binding(root)
    verified = _validation_plan_binding(binding)
    tracker = CompletionTelemetryCollector.for_runtime(verified)
    if type(tracker) is not RuntimeDenominatorTracker:
        raise _error("deepseek_first_live_tracker_invalid")
    return tracker, verified


def _validation_runtime_binding(root: Path) -> VerifiedRuntimeCompletionBinding:
    offline = load_and_select_surface_mapping(
        root,
        "deepseek",
        "responses",
        "openai_compatible_responses",
        purpose="offline_validation",
    )
    selection = surface_mapping.VerifiedSurfaceSelection._create(
        surface_mapping._SELECTION_TOKEN,
        purpose="first_live_validation",
        telemetry_schema_sha256=offline.telemetry_schema_sha256,
        mapping=offline.mapping_snapshot(),
        entry={
            "adapter_version": offline.adapter_version,
            "mapping_version": offline.mapping_version,
            "output_counter_comparability": offline.output_counter_comparability,
            "output_counter_path": offline.output_counter_path,
            "runtime_binding_allowed": True,
        },
    )
    binding = _create_first_live_validation_binding(selection)
    binding.assert_runtime_authority(expected_scope="first_live_validation")
    return binding


def _validation_plan_binding(
    binding: VerifiedRuntimeCompletionBinding,
) -> VerifiedRuntimeDenominatorPlanBinding:
    binding.assert_runtime_authority(expected_scope="first_live_validation")
    snapshot = binding.runtime_snapshot()
    case_ids = [VALIDATION_CASE_ID]
    plan = {
        "schema_version": "provider-completion-runtime-denominator-plan/1.0",
        "provider_id": snapshot["provider_id"],
        "api_surface": snapshot["api_surface"],
        "transport_id": snapshot["transport_id"],
        "adapter_version": snapshot["adapter_version"],
        "telemetry_schema_sha256": snapshot["telemetry_schema_sha256"],
        "mapping_schema_version": snapshot["mapping_schema_version"],
        "mapping_version": snapshot["mapping_version"],
        "mapping_sha256": snapshot["mapping_sha256"],
        "case_ids": case_ids,
        "case_ids_sha256": sha256_json(case_ids),
        "max_turns_per_case": 2,
        "total_model_request_cap": 2,
        "agents_sdk_retries": 0,
        "http_client_retries": 0,
        "denominator_algorithm": "transport-response-finalization-v1",
        "exact_response_count_preregistered": False,
    }
    commitment = sha256_json(plan)
    verified = verify_runtime_denominator_plan(
        binding,
        plan,
        preregistration_commitment=commitment,
    )
    return verified


def _parse_utc(value: object) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise _error("deepseek_first_live_authorization_expiry_invalid", not_run=True)
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError:
        raise _error("deepseek_first_live_authorization_expiry_invalid", not_run=True) from None
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        raise _error("deepseek_first_live_authorization_expiry_invalid", not_run=True)
    return parsed


def _parse_audit_utc(value: object) -> datetime:
    if not isinstance(value, str):
        raise _error("deepseek_first_live_audit_timestamp_invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise _error("deepseek_first_live_audit_timestamp_invalid") from None
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        raise _error("deepseek_first_live_audit_timestamp_invalid")
    return parsed.astimezone(timezone.utc)


def _timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def _clock_now(clock: Callable[[], datetime]) -> datetime:
    try:
        value = clock()
    except Exception:
        raise _error("deepseek_first_live_clock_failed", not_run=True) from None
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise _error("deepseek_first_live_clock_failed", not_run=True)
    return value.astimezone(timezone.utc)


def _monotonic_now(clock: Callable[[], float]) -> float:
    try:
        value = float(clock())
    except Exception:
        raise _error("deepseek_first_live_monotonic_clock_failed", not_run=True) from None
    if not math.isfinite(value) or value < 0:
        raise _error("deepseek_first_live_monotonic_clock_failed", not_run=True)
    return value


def _price(value: object) -> Decimal:
    try:
        price = Decimal(str(value))
        normalized = price.quantize(Decimal("0.000001"))
    except (InvalidOperation, ValueError):
        raise _error("deepseek_first_live_pricing_invalid", not_run=True) from None
    if (
        not price.is_finite()
        or price != normalized
        or normalized <= 0
        or normalized > Decimal("1000")
    ):
        raise _error("deepseek_first_live_pricing_invalid", not_run=True)
    return normalized


def _money(value: Decimal) -> str:
    return format(value.quantize(Decimal("0.000001")), "f")


def _pricing(
    *,
    now: datetime,
    snapshot_date: object,
    source_url: object,
    input_price: object,
    output_price: object,
) -> tuple[date, str, Decimal, Decimal]:
    if not isinstance(snapshot_date, str):
        raise _error("deepseek_first_live_pricing_invalid", not_run=True)
    try:
        resolved_date = date.fromisoformat(snapshot_date)
    except ValueError:
        raise _error("deepseek_first_live_pricing_invalid", not_run=True) from None
    if resolved_date != now.date() or not isinstance(source_url, str):
        raise _error("deepseek_first_live_pricing_stale", not_run=True)
    normalized_source_url = source_url.strip()
    if normalized_source_url != _PRICING_SOURCE_URL:
        raise _error("deepseek_first_live_pricing_source_invalid", not_run=True)
    input_value = _price(input_price)
    output_value = _price(output_price)
    worst = (
        Decimal(1024) * input_value + Decimal(272) * output_value
    ) / Decimal(1_000_000)
    if worst > Decimal("1.000000"):
        raise _error("deepseek_first_live_cost_reservation_exceeded", not_run=True)
    return resolved_date, normalized_source_url, input_value, output_value


def _network_logging_disabled() -> bool:
    return not any(
        logging.getLogger(name).isEnabledFor(logging.DEBUG)
        for name in _NETWORK_LOGGERS
    )


def _environment_isolated(environment: Mapping[str, str]) -> bool:
    return not any(environment.get(name) for name in _FORBIDDEN_OPENAI_ENVIRONMENT)


def _validate_dependencies() -> None:
    expected = {
        "openai": "3.1.0",
        "openai-agents": "0.21.0",
        "httpx": "0.28.1",
    }
    try:
        actual = {name: importlib.metadata.version(name) for name in expected}
        from agents import ModelSettings
        from .model_providers import DeepSeekProvider
    except Exception:
        raise _error("deepseek_first_live_dependency_invalid", not_run=True) from None
    if actual != expected or ModelSettings is None or DeepSeekProvider is None:
        raise _error("deepseek_first_live_dependency_invalid", not_run=True)


def _git_state(root: Path) -> tuple[str, bool, str]:
    try:
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        ).stdout.strip()
        status = subprocess.run(
            ["git", "status", "--porcelain=v1", "--untracked-files=all"],
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        ).stdout
        origin_main = subprocess.run(
            ["git", "rev-parse", "refs/remotes/origin/main"],
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        raise _error("deepseek_first_live_git_state_unavailable", not_run=True) from None
    return head, status == "", origin_main


def _git_file_at_commit(root: Path, commit: str, relative_path: Path) -> bytes:
    try:
        return subprocess.run(
            ["git", "show", f"{commit}:{relative_path.as_posix()}"],
            cwd=root,
            check=True,
            capture_output=True,
            timeout=10,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        raise _error("deepseek_first_live_execution_identity_invalid") from None


def _validate_persisted_execution_identity(
    root: Path,
    consumption: Mapping[str, Any],
) -> None:
    commit = consumption.get("execution_commit")
    source_commitment = consumption.get("source_integrity_commitment_sha256")
    if (
        consumption.get("source_integrity_plan_id") != SOURCE_INTEGRITY_PLAN_ID
        or not isinstance(commit, str)
        or not _GIT_SHA.fullmatch(commit)
        or not isinstance(source_commitment, str)
        or not _SHA256.fullmatch(source_commitment)
    ):
        raise _error("deepseek_first_live_execution_identity_invalid")
    try:
        consumed_at = _parse_utc(consumption.get("consumed_at_utc"))
        pricing_date = date.fromisoformat(str(consumption.get("pricing_snapshot_date")))
    except (DeepSeekFirstLiveValidationError, ValueError):
        raise _error("deepseek_first_live_execution_identity_invalid") from None
    if pricing_date != consumed_at.date():
        raise _error("deepseek_first_live_execution_identity_invalid")
    try:
        ancestor = subprocess.run(
            [
                "git",
                "merge-base",
                "--is-ancestor",
                commit,
                "refs/remotes/origin/main",
            ],
            cwd=root,
            check=False,
            capture_output=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        raise _error("deepseek_first_live_execution_identity_invalid") from None
    if ancestor.returncode != 0:
        raise _error("deepseek_first_live_execution_identity_invalid")
    plan_payload = _git_file_at_commit(root, commit, SOURCE_INTEGRITY_PLAN_RELATIVE_PATH)
    implementation_payload = _git_file_at_commit(root, commit, IMPLEMENTATION_RELATIVE_PATH)
    contract_payload = _git_file_at_commit(root, commit, CONTRACT_RELATIVE_PATH)
    try:
        plan = _decode_json_object(plan_payload)
        from .phase6_depth60 import depth60_successor_v5_plan_commitment_sha256

        recomputed = depth60_successor_v5_plan_commitment_sha256(plan)
    except Exception:
        raise _error("deepseek_first_live_execution_identity_invalid") from None
    authorization_boundary = plan.get("authorization_boundary")
    if (
        plan.get("plan_id") != SOURCE_INTEGRITY_PLAN_ID
        or plan.get("status") != "locked_offline_not_run"
        or plan.get("plan_commitment_sha256") != source_commitment
        or recomputed != source_commitment
        or not isinstance(authorization_boundary, Mapping)
        or authorization_boundary.get("online_execution_authorized")
        is not False
        or _sha256(implementation_payload) != IMPLEMENTATION_FILE_SHA256
        or len(implementation_payload) != IMPLEMENTATION_BYTES
        or _sha256(contract_payload) != CONTRACT_FILE_SHA256
        or len(contract_payload) != CONTRACT_BYTES
    ):
        raise _error("deepseek_first_live_execution_identity_invalid")


def _validate_source_integrity(
    root: Path,
    expected_commitment: str,
) -> dict[str, Any]:
    if not isinstance(expected_commitment, str) or not _SHA256.fullmatch(
        expected_commitment
    ):
        raise _error("deepseek_first_live_source_commitment_invalid", not_run=True)
    try:
        from .phase6_depth60 import validate_phase6_depth60_plan

        result = validate_phase6_depth60_plan(
            root, SOURCE_INTEGRITY_PLAN_RELATIVE_PATH
        )
    except Exception:
        raise _error("deepseek_first_live_source_integrity_invalid", not_run=True) from None
    if (
        result.get("status") != "valid"
        or result.get("plan_id") != SOURCE_INTEGRITY_PLAN_ID
        or result.get("plan_commitment_sha256") != expected_commitment
        or result.get("online_execution_authorized") is not False
        or result.get("network_calls") != 0
        or result.get("model_calls") != 0
    ):
        raise _error("deepseek_first_live_source_integrity_invalid", not_run=True)
    return result


def _authorization_binding(
    *,
    authorization_id: str,
    expires_at_utc: str,
    execution_commit: str,
    source_integrity_commitment: str,
    pricing_snapshot_date: str,
    pricing_source_url: str,
    input_price: Decimal,
    output_price: Decimal,
) -> str:
    return _sha256(
        _canonical_bytes(
            {
                "authorization_id": authorization_id,
                "authorization_expires_at_utc": expires_at_utc,
                "contract_commitment_sha256": CONTRACT_COMMITMENT_SHA256,
                "implementation_commitment_sha256": (
                    IMPLEMENTATION_COMMITMENT_SHA256
                ),
                "execution_commit": execution_commit,
                "source_integrity_commitment_sha256": source_integrity_commitment,
                "pricing_snapshot_date": pricing_snapshot_date,
                "pricing_source_url": pricing_source_url,
                "input_price_per_million_cny": _money(input_price),
                "output_price_per_million_cny": _money(output_price),
                "locked_limits": {
                    "network_attempts": 2,
                    "model_requests": 2,
                    "output_token_caps": [256, 16],
                    "whole_process_wall_timeout_seconds": 330,
                    "local_observed_cost_stop_cny": "1.000000",
                    "retries": 0,
                    "resume": False,
                    "fallback": False,
                },
            }
        )
    )


def _revalidate_execution_state(
    root: Path,
    *,
    expected_source_integrity_commitment: str,
    expected_execution_commit: str,
    git_state_loader: Callable[[Path], tuple[str, bool, str]],
) -> None:
    validate_deepseek_first_live_contract(root)
    validate_deepseek_first_live_implementation(root)
    _validate_source_integrity(root, expected_source_integrity_commitment)
    head, clean, origin_main = git_state_loader(root)
    if (
        head != expected_execution_commit
        or origin_main != expected_execution_commit
        or not clean
    ):
        raise _error("deepseek_first_live_execution_tree_invalid")


def _exclusive_json(path: Path, value: Mapping[str, Any]) -> tuple[bytes, str]:
    body = _canonical_bytes(value) + b"\n"
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(body)
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        try:
            path.unlink(missing_ok=True)
        finally:
            raise
    return body, _sha256(body)


def _read_canonical_json(path: Path) -> tuple[dict[str, Any], bytes]:
    try:
        payload = path.read_bytes()
    except OSError:
        raise _error("deepseek_first_live_artifact_missing") from None
    value = _decode_json_object(payload)
    if payload != _canonical_bytes(value) + b"\n":
        raise _error("deepseek_first_live_artifact_not_canonical")
    return value, payload


def _verify_persisted_ledger(
    ledger: AuditLedger,
    run_entry: Mapping[str, Any],
    denominator: Mapping[str, Any],
    plan_binding: VerifiedRuntimeDenominatorPlanBinding,
    *,
    expected_write_failed: bool,
    expected_run_status: str,
    expected_terminal_error_code: str | None,
    authorization_id_sha256: str,
    expected_network_calls: int | None,
    authorization_expires_at: datetime,
    consumed_at: datetime,
) -> None:
    run_id = run_entry.get("run_id")
    if not isinstance(run_id, str):
        raise _error("deepseek_first_live_audit_index_invalid")
    exported = ledger.export_run(run_id)
    if set(exported) != {
        "schema_version",
        "run",
        "tool_calls",
        "tool_attempts",
        "approval_decisions",
        "model_calls",
        "events",
        "chain_verification",
    }:
        raise _error("deepseek_first_live_audit_export_invalid")
    run = exported.get("run")
    request_summary = {
        "validation_id": VALIDATION_ID,
        "contract_commitment_sha256": CONTRACT_COMMITMENT_SHA256,
        "implementation_commitment_sha256": IMPLEMENTATION_COMMITMENT_SHA256,
        "authorization_id_sha256": authorization_id_sha256,
        "scenario_count": 2,
        "provider": "deepseek",
        "model": "deepseek-v4-flash",
    }
    if (
        not isinstance(run, Mapping)
        or set(run)
        != {
            "run_id",
            "mode",
            "status",
            "request_sha256",
            "dataset_sha256",
            "created_at_utc",
            "updated_at_utc",
            "terminal_error_code",
        }
        or run.get("run_id") != "RUN-DEEPSEEK-FIRST-LIVE-VALIDATION-001"
        or run.get("run_id") != run_id
        or run.get("mode") != "deepseek_completion_first_live_validation"
        or run.get("status") != expected_run_status
        or run.get("terminal_error_code") != expected_terminal_error_code
        or run.get("request_sha256") != sha256_json(request_summary)
        or run.get("dataset_sha256") is not None
        or exported.get("tool_calls") != []
        or exported.get("tool_attempts") != []
        or exported.get("approval_decisions") != []
        or exported.get("model_calls") != []
    ):
        raise _error("deepseek_first_live_audit_run_invalid")
    events = exported.get("events")
    if not isinstance(events, list):
        raise _error("deepseek_first_live_audit_chain_invalid")
    starts: dict[int, tuple[Mapping[str, Any], str]] = {}
    sends: dict[int, tuple[Mapping[str, Any], str, str]] = {}
    terminals: dict[int, tuple[str, Mapping[str, Any], str]] = {}
    for event in events:
        if not isinstance(event, Mapping):
            raise _error("deepseek_first_live_audit_chain_invalid")
        event_type = event.get("event_type")
        payload = event.get("safe_payload")
        event_hash = event.get("event_hash")
        if not isinstance(payload, Mapping) or not isinstance(event_hash, str):
            raise _error("deepseek_first_live_audit_chain_invalid")
        if event_type in {"run_started", "run_status_changed"}:
            continue
        attempt_index = payload.get("attempt_index")
        if type(attempt_index) is not int:
            raise _error("deepseek_first_live_ledger_event_mismatch")
        if event_type == "model_request_started":
            if attempt_index in starts:
                raise _error("deepseek_first_live_ledger_event_mismatch")
            starts[attempt_index] = (payload, event_hash)
        elif event_type == "provider_transport_request_sent":
            if attempt_index in sends:
                raise _error("deepseek_first_live_ledger_event_mismatch")
            occurred_at = event.get("occurred_at_utc")
            if not isinstance(occurred_at, str):
                raise _error("deepseek_first_live_ledger_event_mismatch")
            sends[attempt_index] = (payload, event_hash, occurred_at)
        elif event_type in {
            "model_response_telemetry_recorded",
            COMPLETION_TELEMETRY_UNMAPPED_EVENT,
            "model_response_telemetry_rejected",
            "model_request_http_error",
            "model_request_no_response",
            "model_request_cancelled",
            "model_request_outcome_unknown",
        }:
            if attempt_index in terminals:
                raise _error("deepseek_first_live_ledger_event_mismatch")
            terminals[attempt_index] = (str(event_type), payload, event_hash)
        else:
            raise _error("deepseek_first_live_ledger_event_mismatch")
    attempts = denominator.get("attempts")
    records = denominator.get("records")
    if not isinstance(attempts, list) or not isinstance(records, list):
        raise _error("deepseek_first_live_ledger_event_mismatch")
    binding = plan_binding.runtime_binding().runtime_snapshot()
    records_by_response = {
        item.get("response_index"): item
        for item in records
        if isinstance(item, Mapping) and type(item.get("response_index")) is int
    }
    terminal_types = {
        "response_rejected": "model_response_telemetry_rejected",
        "http_error": "model_request_http_error",
        "no_response": "model_request_no_response",
        "cancelled": "model_request_cancelled",
        "outcome_unknown": "model_request_outcome_unknown",
    }
    ordered_attempts = sorted(attempts, key=lambda item: item.get("attempt_index", -1))
    expected_types = ["run_started"]
    for attempt in ordered_attempts:
        if not isinstance(attempt, Mapping):
            raise _error("deepseek_first_live_ledger_event_mismatch")
        kind = attempt.get("terminal_kind")
        if kind == "response_accepted":
            record = records_by_response.get(attempt.get("response_index"))
            terminal_type = (
                COMPLETION_TELEMETRY_UNMAPPED_EVENT
                if isinstance(record, Mapping)
                and record.get("normalized_completion_state") == "unmapped"
                else "model_response_telemetry_recorded"
            )
        else:
            terminal_type = terminal_types.get(str(kind))
        if terminal_type is None:
            raise _error("deepseek_first_live_ledger_event_mismatch")
        expected_types.append("model_request_started")
        if attempt.get("attempt_index") in sends:
            expected_types.append("provider_transport_request_sent")
        expected_types.append(terminal_type)
    expected_types.append("run_status_changed")
    expected_actors = ["system"] + ["provider_adapter"] * (
        2 * len(attempts) + len(sends)
    ) + ["system"]
    if [event.get("event_type") for event in events] != expected_types:
        raise _error("deepseek_first_live_ledger_event_mismatch")
    expected_run_started = {
        "mode": "deepseek_completion_first_live_validation",
        "request_sha256": sha256_json(request_summary),
        "dataset_sha256": None,
    }
    expected_status_changed = {
        "from": "running",
        "to": expected_run_status,
        "error_code": expected_terminal_error_code,
    }
    if (
        events[0].get("safe_payload") != expected_run_started
        or events[-1].get("safe_payload") != expected_status_changed
        or [event.get("actor_kind") for event in events] != expected_actors
        or any(event.get("run_id") != run_id for event in events)
        or [event.get("sequence") for event in events]
        != list(range(1, len(events) + 1))
    ):
        raise _error("deepseek_first_live_ledger_event_mismatch")
    for attempt in attempts:
        if not isinstance(attempt, Mapping):
            raise _error("deepseek_first_live_ledger_event_mismatch")
        index = attempt.get("attempt_index")
        expected_start = {
            "schema_version": COMPLETION_TELEMETRY_EVENT_SCHEMA_VERSION,
            "case_id": attempt.get("case_id"),
            "attempt_index": index,
            "case_attempt_index": attempt.get("case_attempt_index"),
            "binding": binding,
        }
        actual_start = starts.get(index)
        if actual_start is None or dict(actual_start[0]) != expected_start:
            raise _error("deepseek_first_live_ledger_event_mismatch")
        actual_send = sends.get(index)
        if actual_send is not None:
            expected_send = {
                "schema_version": "deepseek-first-live-transport-send/1.0",
                "case_id": attempt.get("case_id"),
                "attempt_index": index,
                "case_attempt_index": attempt.get("case_attempt_index"),
                "network_call_index": sorted(sends).index(index),
                "method": "POST",
                "origin": "https://api.deepseek.com",
                "path": "/responses",
            }
            try:
                sent_at = _parse_audit_utc(actual_send[2])
            except DeepSeekFirstLiveValidationError:
                raise _error("deepseek_first_live_ledger_event_mismatch") from None
            if (
                dict(actual_send[0]) != expected_send
                or not consumed_at <= sent_at <= authorization_expires_at
            ):
                raise _error("deepseek_first_live_ledger_event_mismatch")
        expected_terminal = {
            "schema_version": COMPLETION_TELEMETRY_EVENT_SCHEMA_VERSION,
            "case_id": attempt.get("case_id"),
            "attempt_index": index,
            "case_attempt_index": attempt.get("case_attempt_index"),
            "terminal_kind": attempt.get("terminal_kind"),
            "response_index": attempt.get("response_index"),
            "error_code": attempt.get("error_code"),
            "binding": binding,
        }
        kind = attempt.get("terminal_kind")
        if kind == "response_accepted":
            record = records_by_response.get(attempt.get("response_index"))
            if not isinstance(record, Mapping):
                raise _error("deepseek_first_live_ledger_event_mismatch")
            expected_terminal["completion_record"] = record
            expected_type = (
                COMPLETION_TELEMETRY_UNMAPPED_EVENT
                if record.get("normalized_completion_state") == "unmapped"
                else "model_response_telemetry_recorded"
            )
        else:
            expected_type = terminal_types.get(str(kind))
        actual_terminal = terminals.get(index)
        if (
            actual_terminal is None
            or actual_terminal[0] != expected_type
            or dict(actual_terminal[1]) != expected_terminal
        ):
            raise _error("deepseek_first_live_ledger_event_mismatch")
    expected_indices = {
        item.get("attempt_index")
        for item in attempts
        if isinstance(item, Mapping)
    }
    if (
        set(starts) != expected_indices
        or set(terminals) != expected_indices
        or not set(sends) <= expected_indices
        or len(sends) != (expected_network_calls or 0)
    ):
        raise _error("deepseek_first_live_ledger_event_mismatch")
    commitment = run_entry.get("completion_telemetry_event_commitment")
    if not isinstance(commitment, Mapping):
        raise _error("deepseek_first_live_event_commitment_invalid")
    commitment_body = {
        "schema_version": "provider-completion-ledger-bridge-commitment/1.0",
        "case_id": VALIDATION_CASE_ID,
        "binding_sha256": sha256_json(binding),
        "started": [
            {"attempt_index": index, "event_hash": starts[index][1]}
            for index in sorted(starts)
        ],
        "terminals": [
            {"attempt_index": index, "event_hash": terminals[index][2]}
            for index in sorted(terminals)
        ],
        "all_started_attempts_terminal": set(starts) == set(terminals),
        "write_failed": expected_write_failed,
    }
    expected_commitment = {
        **commitment_body,
        "commitment_sha256": sha256_json(commitment_body),
    }
    if dict(commitment) != expected_commitment:
        raise _error("deepseek_first_live_event_commitment_invalid")


def _verify_partial_ledger_counts(
    directory: Path,
    terminal: Mapping[str, Any],
    *,
    authorization_id_sha256: str,
    authorization_expires_at: datetime,
    consumed_at: datetime,
) -> None:
    database = directory / "audit.sqlite3"
    if not database.is_file():
        if (
            terminal.get("ledger_run_status") is not None
            or terminal.get("model_requests") != 0
            or terminal.get("network_attempts") != 0
            or terminal.get("network_calls") not in {None, 0}
        ):
            raise _error("deepseek_first_live_partial_ledger_mismatch")
        return
    ledger = AuditLedger(database)
    run_id = "RUN-DEEPSEEK-FIRST-LIVE-VALIDATION-001"
    verification = ledger.verify_chain(run_id)
    if not verification.valid:
        raise _error("deepseek_first_live_partial_ledger_mismatch")
    exported = ledger.export_run(run_id)
    run = exported.get("run")
    events = exported.get("events")
    request_summary = {
        "validation_id": VALIDATION_ID,
        "contract_commitment_sha256": CONTRACT_COMMITMENT_SHA256,
        "implementation_commitment_sha256": IMPLEMENTATION_COMMITMENT_SHA256,
        "authorization_id_sha256": authorization_id_sha256,
        "scenario_count": 2,
        "provider": "deepseek",
        "model": "deepseek-v4-flash",
    }
    if (
        not isinstance(run, Mapping)
        or not isinstance(events, list)
        or run.get("run_id") != run_id
        or run.get("mode") != "deepseek_completion_first_live_validation"
        or run.get("request_sha256") != sha256_json(request_summary)
        or run.get("dataset_sha256") is not None
        or exported.get("tool_calls") != []
        or exported.get("tool_attempts") != []
        or exported.get("approval_decisions") != []
        or exported.get("model_calls") != []
    ):
        raise _error("deepseek_first_live_partial_ledger_mismatch")
    terminal_types = {
        "model_response_telemetry_recorded",
        COMPLETION_TELEMETRY_UNMAPPED_EVENT,
        "model_response_telemetry_rejected",
        "model_request_http_error",
        "model_request_no_response",
        "model_request_cancelled",
        "model_request_outcome_unknown",
    }
    expected_started_payload = {
        "mode": "deepseek_completion_first_live_validation",
        "request_sha256": sha256_json(request_summary),
        "dataset_sha256": None,
    }
    if (
        len(events) < 2
        or events[0].get("event_type") != "run_started"
        or events[0].get("actor_kind") != "system"
        or events[0].get("safe_payload") != expected_started_payload
        or events[-1].get("event_type") != "run_status_changed"
        or events[-1].get("actor_kind") != "system"
        or events[-1].get("safe_payload")
        != {
            "from": "running",
            "to": terminal.get("ledger_run_status"),
            "error_code": (
                None
                if terminal.get("ledger_run_status") == "completed"
                else terminal.get("error_code")
            ),
        }
    ):
        raise _error("deepseek_first_live_partial_ledger_mismatch")
    cursor = 1
    attempt_count = 0
    send_count = 0
    observed_terminal_types: list[str] = []
    while cursor < len(events) - 1:
        start = events[cursor]
        start_payload = start.get("safe_payload")
        if (
            start.get("event_type") != "model_request_started"
            or start.get("actor_kind") != "provider_adapter"
            or not isinstance(start_payload, Mapping)
            or start_payload.get("case_id") != VALIDATION_CASE_ID
            or start_payload.get("attempt_index") != attempt_count
            or start_payload.get("case_attempt_index") != attempt_count
        ):
            raise _error("deepseek_first_live_partial_ledger_mismatch")
        cursor += 1
        if (
            cursor < len(events) - 1
            and events[cursor].get("event_type")
            == "provider_transport_request_sent"
        ):
            sent = events[cursor]
            sent_payload = sent.get("safe_payload")
            if (
                sent.get("actor_kind") != "provider_adapter"
                or not isinstance(sent_payload, Mapping)
                or sent_payload
                != {
                    "schema_version": "deepseek-first-live-transport-send/1.0",
                    "case_id": VALIDATION_CASE_ID,
                    "attempt_index": attempt_count,
                    "case_attempt_index": attempt_count,
                    "network_call_index": send_count,
                    "method": "POST",
                    "origin": "https://api.deepseek.com",
                    "path": "/responses",
                }
            ):
                raise _error("deepseek_first_live_partial_ledger_mismatch")
            try:
                sent_at = _parse_audit_utc(sent.get("occurred_at_utc"))
            except DeepSeekFirstLiveValidationError:
                raise _error("deepseek_first_live_partial_ledger_mismatch") from None
            if not consumed_at <= sent_at <= authorization_expires_at:
                raise _error("deepseek_first_live_partial_ledger_mismatch")
            send_count += 1
            cursor += 1
        if cursor >= len(events) - 1:
            raise _error("deepseek_first_live_partial_ledger_mismatch")
        terminal_event = events[cursor]
        terminal_payload = terminal_event.get("safe_payload")
        terminal_type = terminal_event.get("event_type")
        if (
            terminal_type not in terminal_types
            or terminal_event.get("actor_kind") != "provider_adapter"
            or not isinstance(terminal_payload, Mapping)
            or terminal_payload.get("case_id") != VALIDATION_CASE_ID
            or terminal_payload.get("attempt_index") != attempt_count
            or terminal_payload.get("case_attempt_index") != attempt_count
        ):
            raise _error("deepseek_first_live_partial_ledger_mismatch")
        observed_terminal_types.append(str(terminal_type))
        attempt_count += 1
        cursor += 1
    if (
        cursor != len(events) - 1
        or run.get("status") != terminal.get("ledger_run_status")
        or run.get("terminal_error_code")
        != (
            None
            if terminal.get("ledger_run_status") == "completed"
            else terminal.get("error_code")
        )
        or attempt_count != terminal.get("model_requests")
        or send_count != terminal.get("network_attempts")
        or (
            terminal.get("network_call_observation_complete") is True
            and send_count != terminal.get("network_calls")
        )
        or (
            terminal.get("network_call_observation_complete") is False
            and (send_count != 0 or terminal.get("network_calls") is not None)
        )
        or (
            terminal.get("ledger_run_status") == "completed"
            and (
                attempt_count != 2
                or send_count != 2
                or observed_terminal_types
                != [
                    "model_response_telemetry_recorded",
                    "model_response_telemetry_recorded",
                ]
            )
        )
    ):
        raise _error("deepseek_first_live_partial_ledger_mismatch")


def _artifact_parent(root: Path, override: Path | None) -> Path:
    if override is not None:
        if _path_is_link_like(override):
            raise _error("deepseek_first_live_artifact_path_invalid")
        parent = override.resolve()
    else:
        relative = PurePosixPath(
            "artifacts/provider_completion_first_live_validation/"
            "deepseek-responses-v1"
        )
        lexical = root.joinpath(*relative.parts)
        current = root
        for part in relative.parts:
            current = current / part
            if current.exists() and _path_is_link_like(current):
                raise _error("deepseek_first_live_artifact_path_invalid")
        parent = lexical.resolve()
        if not parent.is_relative_to(root):
            raise _error("deepseek_first_live_artifact_path_invalid")
    return parent


def _artifact_directory(
    root: Path,
    authorization_id_sha256: str,
    override: Path | None,
) -> Path:
    parent = _artifact_parent(root, override)
    parent.mkdir(parents=True, exist_ok=True)
    directory = parent / authorization_id_sha256
    try:
        directory.mkdir(exist_ok=False)
    except FileExistsError:
        raise _error("deepseek_first_live_authorization_already_consumed", not_run=True) from None
    except OSError:
        raise _error("deepseek_first_live_consumption_directory_failed", not_run=True) from None
    return directory


def _path_is_link_like(path: Path) -> bool:
    try:
        is_junction = getattr(path, "is_junction", None)
        return path.is_symlink() or bool(callable(is_junction) and is_junction())
    except OSError:
        return True


def _validated_artifact_entries(directory: Path) -> tuple[Path, ...]:
    try:
        entries = tuple(directory.iterdir())
        resolved_directory = directory.resolve(strict=True)
    except OSError:
        raise _error("deepseek_first_live_artifact_path_invalid") from None
    for entry in entries:
        try:
            valid = (
                not _path_is_link_like(entry)
                and entry.is_file()
                and entry.name in _ARTIFACT_FILENAMES
                and entry.resolve(strict=True).parent == resolved_directory
            )
        except OSError:
            valid = False
        if not valid:
            raise _error("deepseek_first_live_artifact_file_set_invalid")
    return entries


def _partial_artifact_snapshot(directory: Path) -> dict[str, dict[str, Any]]:
    entries = _validated_artifact_entries(directory)
    snapshot: dict[str, dict[str, Any]] = {}
    for path in sorted(entries, key=lambda item: item.name):
        if path.name == "terminal.json":
            continue
        try:
            payload = path.read_bytes()
        except OSError:
            raise _error("deepseek_first_live_partial_artifact_read_failed") from None
        snapshot[path.name] = {"bytes": len(payload), "sha256": _sha256(payload)}
    return snapshot


def _verify_deepseek_first_live_artifacts_impl(
    project_root: str | Path,
    authorization_id_sha256: str,
    *,
    sensitive_canaries: tuple[str, ...] = (),
    _artifact_root: Path | None = None,
) -> dict[str, Any]:
    """Verify one persisted validation receipt without reading a Provider Key."""

    root = Path(project_root).resolve()
    validate_deepseek_first_live_contract(root)
    validate_deepseek_first_live_implementation(root)
    if not isinstance(authorization_id_sha256, str) or not _SHA256.fullmatch(
        authorization_id_sha256
    ):
        raise _error("deepseek_first_live_authorization_hash_invalid")
    parent = _artifact_parent(root, _artifact_root)
    directory = parent / authorization_id_sha256
    if (
        _path_is_link_like(directory)
        or not directory.is_dir()
        or directory.resolve().parent != parent.resolve()
    ):
        raise _error("deepseek_first_live_artifact_path_invalid")
    entries = _validated_artifact_entries(directory)
    names = {item.name for item in entries}
    for path in entries:
        payload = path.read_bytes()
        if any(pattern.search(payload) for pattern in _FORBIDDEN_ARTIFACT_PATTERNS):
            raise _error("deepseek_first_live_sensitive_artifact")
    consumption, consumption_bytes = _read_canonical_json(
        directory / "consumption.json"
    )
    terminal, terminal_bytes = _read_canonical_json(directory / "terminal.json")
    _require_exact_fields(
        consumption,
        _CONSUMPTION_FIELDS,
        "deepseek_first_live_consumption_schema_invalid",
    )
    _require_exact_fields(
        terminal,
        _TERMINAL_FIELDS,
        "deepseek_first_live_terminal_schema_invalid",
    )
    _validate_persisted_execution_identity(root, consumption)
    terminal_status = terminal.get("status")
    terminal_error = terminal.get("error_code")
    if (
        consumption.get("schema_version") != CONSUMPTION_SCHEMA_VERSION
        or consumption.get("status") != "consumed"
        or consumption.get("contract_id") != VALIDATION_ID
        or consumption.get("contract_commitment_sha256")
        != CONTRACT_COMMITMENT_SHA256
        or consumption.get("authorization_id_sha256") != authorization_id_sha256
        or terminal.get("schema_version") != TERMINAL_SCHEMA_VERSION
        or terminal.get("contract_id") != VALIDATION_ID
        or terminal.get("authorization_id_sha256") != authorization_id_sha256
        or terminal.get("contract_commitment_sha256")
        != CONTRACT_COMMITMENT_SHA256
        or consumption.get("implementation_commitment_sha256")
        != IMPLEMENTATION_COMMITMENT_SHA256
        or terminal.get("implementation_commitment_sha256")
        != IMPLEMENTATION_COMMITMENT_SHA256
        or terminal.get("consumption_receipt_sha256")
        != _sha256(consumption_bytes)
        or terminal.get("authorization_binding_sha256")
        != consumption.get("authorization_binding_sha256")
        or terminal.get("execution_commit") != consumption.get("execution_commit")
        or terminal.get("source_integrity_commitment_sha256")
        != consumption.get("source_integrity_commitment_sha256")
        or terminal.get("source_integrity_plan_id")
        != consumption.get("source_integrity_plan_id")
        or terminal.get("authorization_expires_at_utc")
        != consumption.get("authorization_expires_at_utc")
        or consumption.get("pricing_source_url") != _PRICING_SOURCE_URL
        or consumption.get("consume_before_key_load") is not True
        or consumption.get("provider_key_loaded_at_consumption") is not False
        or consumption.get("network_attempts_at_consumption") != 0
        or consumption.get("model_requests_at_consumption") != 0
        or consumption.get("authorizes_retry") is not False
        or consumption.get("authorizes_resume") is not False
        or consumption.get("authorizes_evaluation") is not False
        or consumption.get("authorizes_status_closure") is not False
        or terminal.get("closure_claim_allowed") is not False
        or terminal.get("automatic_registry_promotion_allowed") is not False
        or (
            terminal_status == "failed"
            and (
                not isinstance(terminal_error, str)
                or not _SAFE_ERROR.fullmatch(terminal_error)
            )
        )
    ):
        raise _error("deepseek_first_live_receipt_binding_invalid")
    try:
        authorization_expires_at = _parse_utc(
            consumption.get("authorization_expires_at_utc")
        )
        consumed_at = _parse_utc(consumption.get("consumed_at_utc"))
        started_at = _parse_utc(terminal.get("started_at_utc"))
        completed_at = _parse_utc(terminal.get("completed_at_utc"))
        _price(consumption.get("input_price_per_million_cny"))
        _price(consumption.get("output_price_per_million_cny"))
        date.fromisoformat(str(consumption.get("pricing_snapshot_date")))
    except (DeepSeekFirstLiveValidationError, ValueError):
        raise _error("deepseek_first_live_receipt_binding_invalid") from None
    if (
        not consumed_at <= started_at <= completed_at <= authorization_expires_at
        or authorization_expires_at - consumed_at
        < timedelta(seconds=_WHOLE_PROCESS_TIMEOUT_SECONDS)
        or authorization_expires_at - consumed_at > timedelta(hours=24)
        or completed_at - started_at
        > timedelta(seconds=_WHOLE_PROCESS_TIMEOUT_SECONDS)
    ):
        raise _error("deepseek_first_live_receipt_time_order_invalid")
    network_observation_complete = terminal.get("network_call_observation_complete")
    network_calls = terminal.get("network_calls")
    network_attempts = terminal.get("network_attempts")
    model_requests = terminal.get("model_requests")
    partial_artifacts = terminal.get("partial_artifacts")
    input_tokens = terminal.get("input_tokens")
    output_tokens = terminal.get("output_tokens")
    usage_complete = terminal.get("usage_complete")
    observed_cost_value = terminal.get("local_observed_usage_cost_cny")
    expected_observed_cost: str | None = None
    if type(input_tokens) is int and type(output_tokens) is int:
        expected_observed_cost = _money(
            (
                Decimal(input_tokens)
                * _price(consumption.get("input_price_per_million_cny"))
                + Decimal(output_tokens)
                * _price(consumption.get("output_price_per_million_cny"))
            )
            / Decimal(1_000_000)
        )
    if (
        terminal_status not in {"success", "failed"}
        or type(terminal.get("outcome_unknown")) is not bool
        or type(network_attempts) is not int
        or not 0 <= network_attempts <= 2
        or type(model_requests) is not int
        or not 0 <= model_requests <= 2
        or network_attempts > model_requests
        or type(network_observation_complete) is not bool
        or (
            network_observation_complete
            and (type(network_calls) is not int or network_calls != network_attempts)
        )
        or (not network_observation_complete and network_calls is not None)
        or terminal.get("network_attempt_limit") != 2
        or terminal.get("model_request_limit") != 2
        or terminal.get("input_token_limit") != 1024
        or terminal.get("output_token_limit") != 272
        or not (
            input_tokens is None
            or (type(input_tokens) is int and 0 <= input_tokens <= 1024)
        )
        or not (
            output_tokens is None
            or (type(output_tokens) is int and 0 <= output_tokens <= 272)
        )
        or type(usage_complete) is not bool
        or usage_complete
        != (type(input_tokens) is int and type(output_tokens) is int)
        or observed_cost_value != expected_observed_cost
        or type(terminal.get("provider_key_loaded")) is not bool
        or terminal.get("provider_key_persisted") is not False
        or terminal.get("raw_response_body_persisted") is not False
        or terminal.get("message_content_persisted") is not False
        or terminal.get("exception_text_persisted") is not False
        or terminal.get("input_content_repeated_in_receipt") is not False
        or terminal.get("provider_key_persisted") is not False
        or terminal.get("strict_provider_billing_hard_cap") is not False
        or terminal.get("actual_provider_billed_cost_cny") is not None
        or terminal.get("local_observed_usage_cost_stop_cny") != "1.000000"
        or type(terminal.get("raw_response_cleanup_complete")) is not bool
        or not isinstance(partial_artifacts, dict)
        or type(terminal.get("manifest_complete")) is not bool
        or not isinstance(terminal.get("authorization_binding_sha256"), str)
        or not _SHA256.fullmatch(terminal["authorization_binding_sha256"])
        or not isinstance(terminal.get("source_integrity_commitment_sha256"), str)
        or not _SHA256.fullmatch(terminal["source_integrity_commitment_sha256"])
        or not isinstance(terminal.get("execution_commit"), str)
        or not _GIT_SHA.fullmatch(terminal["execution_commit"])
        or terminal.get("ledger_run_status")
        not in {None, "completed", "failed", "cancelled"}
        or terminal.get("closure_claim_allowed") is not False
        or terminal.get("automatic_registry_promotion_allowed") is not False
        or terminal.get("authorizes_retry") is not False
        or terminal.get("authorizes_resume") is not False
        or terminal.get("authorizes_evaluation") is not False
        or terminal.get("authorizes_provider_registration") is not False
        or terminal.get("authorizes_model_quality_claim") is not False
    ):
        raise _error("deepseek_first_live_terminal_invalid")
    for canary in sensitive_canaries:
        if not isinstance(canary, str) or not canary:
            raise _error("deepseek_first_live_canary_invalid")
        needle = canary.encode("utf-8")
        for path in entries:
            if needle in path.read_bytes():
                raise _error("deepseek_first_live_sensitive_artifact")
    if terminal.get("manifest_complete") is not True:
        if (
            terminal.get("status") == "success"
            or terminal.get("manifest_sha256") is not None
            or set(names) != {"consumption.json", "terminal.json"} | set(partial_artifacts)
        ):
            raise _error("deepseek_first_live_manifest_invalid")
        for filename, expected in partial_artifacts.items():
            if (
                filename in {"terminal.json"}
                or filename not in _ARTIFACT_FILENAMES
                or not isinstance(expected, Mapping)
                or set(expected) != {"bytes", "sha256"}
            ):
                raise _error("deepseek_first_live_partial_manifest_invalid")
            path = directory / filename
            payload = path.read_bytes()
            if expected != {"bytes": len(payload), "sha256": _sha256(payload)}:
                raise _error("deepseek_first_live_partial_manifest_invalid")
            if path.suffix == ".json":
                _read_canonical_json(path)
        _verify_partial_ledger_counts(
            directory,
            terminal,
            authorization_id_sha256=authorization_id_sha256,
            authorization_expires_at=authorization_expires_at,
            consumed_at=consumed_at,
        )
        return {
            "status": "valid_failure_receipt",
            "terminal_status": terminal.get("status"),
            "error_code": terminal.get("error_code"),
            "outcome_unknown": terminal.get("outcome_unknown"),
            "manifest_complete": False,
            "closure_claim_allowed": False,
            "network_attempts": network_attempts,
            "network_calls": network_calls,
            "network_call_observation_complete": network_observation_complete,
            "verification_network_calls": 0,
        }

    required_names = set(_ARTIFACT_FILENAMES)
    if names != required_names or terminal.get("ledger_run_status") is None:
        raise _error("deepseek_first_live_artifact_file_set_invalid")
    manifest, manifest_bytes = _read_canonical_json(directory / "manifest.json")
    _require_exact_fields(
        manifest,
        _MANIFEST_FIELDS,
        "deepseek_first_live_manifest_schema_invalid",
    )
    if (
        manifest.get("schema_version") != MANIFEST_SCHEMA_VERSION
        or manifest.get("status") != "complete"
        or manifest.get("contract_commitment_sha256")
        != CONTRACT_COMMITMENT_SHA256
        or manifest.get("implementation_commitment_sha256")
        != IMPLEMENTATION_COMMITMENT_SHA256
        or manifest.get("raw_response_body_persisted") is not False
        or manifest.get("message_content_persisted") is not False
        or manifest.get("api_key_persisted") is not False
        or terminal.get("manifest_sha256") != _sha256(manifest_bytes)
        or partial_artifacts != {}
        or not isinstance(manifest.get("files"), dict)
    ):
        raise _error("deepseek_first_live_manifest_invalid")
    expected_manifest_files = {
        "consumption.json",
        "audit.sqlite3",
        "audit_index.json",
        "runtime_denominator.json",
        "completion_telemetry.json",
    }
    if set(manifest["files"]) != expected_manifest_files:
        raise _error("deepseek_first_live_manifest_invalid")
    for filename, commitment in manifest["files"].items():
        if not isinstance(commitment, dict) or set(commitment) != {"bytes", "sha256"}:
            raise _error("deepseek_first_live_manifest_invalid")
        payload = (directory / filename).read_bytes()
        if (
            commitment.get("bytes") != len(payload)
            or commitment.get("sha256") != _sha256(payload)
        ):
            raise _error("deepseek_first_live_artifact_hash_mismatch")
    audit_index, _ = _read_canonical_json(directory / "audit_index.json")
    denominator, _ = _read_canonical_json(directory / "runtime_denominator.json")
    evidence, _ = _read_canonical_json(directory / "completion_telemetry.json")
    _require_exact_fields(
        audit_index,
        _AUDIT_INDEX_FIELDS,
        "deepseek_first_live_audit_index_invalid",
    )
    runs = audit_index.get("runs")
    if not isinstance(runs, list) or len(runs) != 1 or not isinstance(runs[0], dict):
        raise _error("deepseek_first_live_audit_index_invalid")
    _require_exact_fields(
        runs[0],
        _AUDIT_RUN_ENTRY_FIELDS,
        "deepseek_first_live_audit_index_invalid",
    )
    if runs[0].get("case_id") != VALIDATION_CASE_ID:
        raise _error("deepseek_first_live_audit_index_invalid")
    run_id = runs[0].get("run_id")
    if not isinstance(run_id, str):
        raise _error("deepseek_first_live_audit_index_invalid")
    ledger = AuditLedger(directory / "audit.sqlite3")
    verification = ledger.verify_chain(run_id)
    if not verification.valid or runs[0].get("chain_verification") != verification.to_dict():
        raise _error("deepseek_first_live_audit_chain_invalid")
    binding = _validation_runtime_binding(root)
    plan_binding = _validation_plan_binding(binding)
    validate_runtime_denominator_artifact(denominator, plan_binding=plan_binding)
    ledger_reconciliation = evidence.get("ledger_reconciliation")
    _require_exact_fields(
        evidence,
        (
            _SUCCESS_EVIDENCE_FIELDS
            if terminal_status == "success"
            else _FAILURE_EVIDENCE_FIELDS
        ),
        "deepseek_first_live_evidence_schema_invalid",
    )
    if not isinstance(ledger_reconciliation, Mapping) or type(
        ledger_reconciliation.get("ledger_failure_observed")
    ) is not bool:
        raise _error("deepseek_first_live_evidence_binding_invalid")
    _verify_persisted_ledger(
        ledger,
        runs[0],
        denominator,
        plan_binding,
        expected_write_failed=ledger_reconciliation["ledger_failure_observed"],
        expected_run_status=terminal.get("ledger_run_status"),
        expected_terminal_error_code=(
            None
            if terminal.get("ledger_run_status") == "completed"
            else terminal_error
        ),
        authorization_id_sha256=authorization_id_sha256,
        expected_network_calls=network_calls,
        authorization_expires_at=authorization_expires_at,
        consumed_at=consumed_at,
    )
    if (
        evidence.get("runtime_denominator") != denominator
        or evidence.get("runtime_plan") != plan_binding_to_snapshot(plan_binding)
        or evidence.get("schema_version") != EVIDENCE_SCHEMA_VERSION
        or evidence.get("contract_id") != VALIDATION_ID
        or evidence.get("contract_commitment_sha256")
        != CONTRACT_COMMITMENT_SHA256
        or evidence.get("implementation_commitment_sha256")
        != IMPLEMENTATION_COMMITMENT_SHA256
        or evidence.get("status_defect_closure_allowed") is not False
        or evidence.get("closure_claim_allowed") is not False
        or evidence.get("automatic_registry_promotion_allowed") is not False
        or evidence.get("raw_response_body_persisted") is not False
        or evidence.get("message_content_persisted") is not False
        or evidence.get("provider_key_persisted") is not False
    ):
        raise _error("deepseek_first_live_evidence_binding_invalid")
    if terminal.get("status") == "success":
        records = denominator.get("records")
        input_price = _price(consumption.get("input_price_per_million_cny"))
        output_price = _price(consumption.get("output_price_per_million_cny"))
        input_tokens, output_tokens, observed_cost = _validated_success_usage(
            records,
            input_price=input_price,
            output_price=output_price,
            error_code="deepseek_first_live_success_evidence_invalid",
        )
        recomputed_usage = (input_tokens, output_tokens)
        recomputed_cost = _money(observed_cost)
        recomputed_states = [
            item.get("normalized_completion_state") for item in records
        ]
        recomputed_sources = [
            item.get("truncation_signal_source") for item in records
        ]
        if (
            terminal_error is not None
            or terminal.get("outcome_unknown") is not False
            or terminal.get("network_attempts") != 2
            or terminal.get("network_calls") != 2
            or terminal.get("network_call_observation_complete") is not True
            or terminal.get("model_requests") != 2
            or terminal.get("provider_key_loaded") is not True
            or terminal.get("raw_response_cleanup_complete") is not True
            or terminal.get("ledger_run_status") != "completed"
            or terminal.get("usage_complete") is not True
            or evidence.get("status") != "validated"
            or evidence.get("validation_integrity_gate_passed") is not True
            or not isinstance(records, list)
            or recomputed_states != ["completed", "incomplete_length"]
            or recomputed_sources != ["native_status", "native_status"]
            or evidence.get("observed_states_in_order") != recomputed_states
            or evidence.get("observed_signal_sources_in_order")
            != recomputed_sources
            or [_completion_shape_projection(item) for item in records]
            != _EXPECTED_COMPLETION_SHAPES
            or evidence.get("observed_completion_shapes_in_order")
            != _EXPECTED_COMPLETION_SHAPES
            or recomputed_usage
            != (
                evidence.get("observed_input_tokens"),
                evidence.get("observed_output_tokens"),
            )
            or terminal.get("input_tokens") != evidence.get("observed_input_tokens")
            or terminal.get("output_tokens") != evidence.get("observed_output_tokens")
            or terminal.get("local_observed_usage_cost_cny")
            != evidence.get("local_observed_cost_cny")
            or evidence.get("local_observed_cost_cny") != recomputed_cost
            or ledger_reconciliation.get("all_chains_valid") is not True
            or ledger_reconciliation.get("ledger_export_failed") is not False
            or ledger_reconciliation.get("ledger_failure_observed") is not False
            or ledger_reconciliation.get("reasons") != []
        ):
            raise _error("deepseek_first_live_success_evidence_invalid")
    elif (
        not isinstance(terminal_error, str)
        or not _SAFE_ERROR.fullmatch(terminal_error)
        or evidence.get("status") != "failed"
        or evidence.get("error_code") != terminal_error
        or evidence.get("outcome_unknown") != terminal.get("outcome_unknown")
        or evidence.get("validation_integrity_gate_passed") is not False
    ):
        raise _error("deepseek_first_live_failure_evidence_invalid")
    return {
        "status": "valid",
        "terminal_status": terminal.get("status"),
        "error_code": terminal.get("error_code"),
        "outcome_unknown": terminal.get("outcome_unknown"),
        "manifest_complete": True,
        "authorization_id_sha256": authorization_id_sha256,
        "manifest_sha256": _sha256(manifest_bytes),
        "terminal_receipt_sha256": _sha256(terminal_bytes),
        "closure_claim_allowed": False,
        "network_attempts": network_attempts,
        "network_calls": network_calls,
        "network_call_observation_complete": network_observation_complete,
        "verification_network_calls": 0,
    }


def verify_deepseek_first_live_artifacts(
    project_root: str | Path,
    authorization_id_sha256: str,
    *,
    sensitive_canaries: tuple[str, ...] = (),
    _artifact_root: Path | None = None,
) -> dict[str, Any]:
    try:
        return _verify_deepseek_first_live_artifacts_impl(
            project_root,
            authorization_id_sha256,
            sensitive_canaries=sensitive_canaries,
            _artifact_root=_artifact_root,
        )
    except DeepSeekFirstLiveValidationError:
        raise
    except Exception:
        raise _error("deepseek_first_live_artifact_verification_failed") from None


def plan_binding_to_snapshot(
    plan_binding: VerifiedRuntimeDenominatorPlanBinding,
) -> dict[str, Any]:
    plan_binding.assert_plan_authority()
    return {
        "case_ids": list(plan_binding.case_ids),
        "case_ids_sha256": plan_binding.case_ids_sha256,
        "max_turns_per_case": plan_binding.max_turns_per_case,
        "total_model_request_cap": plan_binding.total_model_request_cap,
        "preregistration_commitment": plan_binding.preregistration_commitment,
        "denominator_algorithm": plan_binding.denominator_algorithm,
        "exact_response_count_preregistered": (
            plan_binding.exact_response_count_preregistered
        ),
        "binding": plan_binding.runtime_binding().runtime_snapshot(),
    }


def _not_run(code: str) -> dict[str, Any]:
    return {
        "schema_version": "deepseek-first-live-cli-gate/1.0",
        "status": "not_run",
        "error_code": code,
        "outcome_unknown": False,
        "contract_id": VALIDATION_ID,
        "contract_commitment_sha256": CONTRACT_COMMITMENT_SHA256,
        "implementation_commitment_sha256": IMPLEMENTATION_COMMITMENT_SHA256,
        "authorization_consumed": False,
        "provider_key_loaded": False,
        "network_attempts": 0,
        "network_calls": 0,
        "model_requests": 0,
        "closure_claim_allowed": False,
        "automatic_registry_promotion_allowed": False,
    }


def _write_evidence_artifacts(
    directory: Path,
    *,
    consumption_sha256: str,
    ledger: AuditLedger,
    audit_index: list[dict[str, Any]],
    completion: Mapping[str, Any],
    runtime_plan_binding: VerifiedRuntimeDenominatorPlanBinding,
) -> tuple[dict[str, Any], str]:
    runtime_denominator = completion.get("runtime_denominator")
    if not isinstance(runtime_denominator, Mapping):
        raise _error("deepseek_first_live_runtime_denominator_missing")
    validate_runtime_denominator_artifact(
        runtime_denominator,
        plan_binding=runtime_plan_binding,
    )
    files: dict[str, dict[str, Any]] = {}
    for filename, payload in (
        ("audit_index.json", {"runs": audit_index}),
        ("runtime_denominator.json", dict(runtime_denominator)),
        ("completion_telemetry.json", dict(completion)),
    ):
        body, digest = _exclusive_json(directory / filename, payload)
        files[filename] = {"bytes": len(body), "sha256": digest}
    database = directory / "audit.sqlite3"
    database_bytes = database.read_bytes()
    files["audit.sqlite3"] = {
        "bytes": len(database_bytes),
        "sha256": _sha256(database_bytes),
    }
    files["consumption.json"] = {
        "sha256": consumption_sha256,
        "bytes": (directory / "consumption.json").stat().st_size,
    }
    manifest = {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "status": "complete",
        "contract_commitment_sha256": CONTRACT_COMMITMENT_SHA256,
        "implementation_commitment_sha256": IMPLEMENTATION_COMMITMENT_SHA256,
        "files": files,
        "raw_response_body_persisted": False,
        "message_content_persisted": False,
        "api_key_persisted": False,
    }
    body, digest = _exclusive_json(directory / "manifest.json", manifest)
    return {**manifest, "bytes": len(body)}, digest


def _failed_evidence(
    outer: Mapping[str, Any],
    *,
    error_code: str | None,
    outcome_unknown: bool,
) -> dict[str, Any]:
    return {
        "schema_version": EVIDENCE_SCHEMA_VERSION,
        "status": "failed",
        "error_code": error_code,
        "outcome_unknown": outcome_unknown,
        "contract_id": VALIDATION_ID,
        "contract_commitment_sha256": CONTRACT_COMMITMENT_SHA256,
        "implementation_commitment_sha256": IMPLEMENTATION_COMMITMENT_SHA256,
        "runtime_plan": outer["runtime_plan"],
        "runtime_denominator": outer["runtime_denominator"],
        "ledger_reconciliation": outer["ledger_reconciliation"],
        "validation_integrity_gate_passed": False,
        "status_defect_closure_allowed": False,
        "closure_claim_allowed": False,
        "automatic_registry_promotion_allowed": False,
        "raw_response_body_persisted": False,
        "message_content_persisted": False,
        "provider_key_persisted": False,
    }


def _usage_totals(records: object) -> tuple[int, int] | None:
    if not isinstance(records, list) or not records:
        return None
    input_total = 0
    output_total = 0
    for record in records:
        if not isinstance(record, Mapping):
            return None
        usage = record.get("usage")
        if not isinstance(usage, Mapping) or usage.get("complete") is not True:
            return None
        normalized = usage.get("normalized")
        if not isinstance(normalized, Mapping):
            return None
        input_tokens = normalized.get("input_tokens")
        output_tokens = normalized.get("output_tokens")
        if (
            type(input_tokens) is not int
            or input_tokens < 0
            or type(output_tokens) is not int
            or output_tokens < 0
        ):
            return None
        input_total += input_tokens
        output_total += output_tokens
    return input_total, output_total


def _validated_success_usage(
    records: object,
    *,
    input_price: Decimal,
    output_price: Decimal,
    error_code: str,
) -> tuple[int, int, Decimal]:
    if not isinstance(records, list) or len(records) != 2:
        raise _error(error_code)
    totals = _usage_totals(records)
    if totals is None:
        raise _error(error_code)
    for record, output_cap in zip(records, (256, 16)):
        normalized = record.get("usage", {}).get("normalized")
        if (
            not isinstance(normalized, Mapping)
            or type(normalized.get("input_tokens")) is not int
            or normalized["input_tokens"] > 512
            or type(normalized.get("output_tokens")) is not int
            or normalized["output_tokens"] > output_cap
        ):
            raise _error(error_code)
    input_tokens, output_tokens = totals
    observed_cost = (
        Decimal(input_tokens) * input_price
        + Decimal(output_tokens) * output_price
    ) / Decimal(1_000_000)
    if (
        input_tokens > 1024
        or output_tokens > 272
        or observed_cost > Decimal("1.000000")
    ):
        raise _error(error_code)
    return input_tokens, output_tokens, observed_cost


def _completion_shape_projection(record: Mapping[str, Any]) -> dict[str, Any]:
    status = record.get("native_status")
    details = record.get("native_incomplete_details")
    if not isinstance(status, Mapping) or not isinstance(details, Mapping):
        raise _error("deepseek_first_live_completion_projection_invalid")
    return {
        "status_availability": status.get("availability"),
        "status_value": status.get("value"),
        "incomplete_details_availability": details.get("availability"),
        "incomplete_details_value": details.get("value"),
    }


_EXPECTED_COMPLETION_SHAPES = [
    {
        "status_availability": "provided",
        "status_value": "completed",
        "incomplete_details_availability": "provided",
        "incomplete_details_value": None,
    },
    {
        "status_availability": "provided",
        "status_value": "incomplete",
        "incomplete_details_availability": "provided",
        "incomplete_details_value": {"reason": "max_output_tokens"},
    },
]


async def _run_deepseek_first_live_validation_impl(
    *,
    project_root: str | Path,
    authorization_id: str | None,
    authorization_expires_at_utc: str | None,
    expected_contract_commitment_sha256: str | None,
    expected_source_integrity_commitment_sha256: str | None,
    expected_execution_commit: str | None,
    pricing_snapshot_date: str | None,
    pricing_source_url: str | None,
    input_price_per_million_cny: str | Decimal | None,
    output_price_per_million_cny: str | Decimal | None,
    confirm_online: bool,
    accept_locked_caps: bool,
    attest_pricing_current: bool,
    _key_loader: Callable[[], str | None] | None = None,
    _clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    _git_state_loader: Callable[[Path], tuple[str, bool, str]] = _git_state,
    _artifact_root: Path | None = None,
    _monotonic: Callable[[], float] = time.monotonic,
) -> dict[str, Any]:
    """Consume one grant and run exactly two fixed Adapter-path shape calls."""

    try:
        wall_started = _monotonic_now(_monotonic)
    except DeepSeekFirstLiveValidationError:
        return _not_run("deepseek_first_live_monotonic_clock_failed")
    if confirm_online is not True:
        return _not_run("deepseek_first_live_confirmation_required")
    if accept_locked_caps is not True:
        return _not_run("deepseek_first_live_locked_caps_not_accepted")
    if attest_pricing_current is not True:
        return _not_run("deepseek_first_live_pricing_attestation_required")
    if expected_contract_commitment_sha256 != CONTRACT_COMMITMENT_SHA256:
        return _not_run("deepseek_first_live_contract_not_authorized")
    if (
        not isinstance(expected_source_integrity_commitment_sha256, str)
        or not _SHA256.fullmatch(expected_source_integrity_commitment_sha256)
    ):
        return _not_run("deepseek_first_live_source_commitment_invalid")
    if (
        not isinstance(expected_execution_commit, str)
        or not _GIT_SHA.fullmatch(expected_execution_commit)
    ):
        return _not_run("deepseek_first_live_execution_commit_invalid")
    if (
        not isinstance(authorization_id, str)
        or not _AUTHORIZATION_ID.fullmatch(authorization_id)
        or not isinstance(authorization_expires_at_utc, str)
    ):
        return _not_run("deepseek_first_live_authorization_invalid")

    root = Path(project_root).resolve()
    try:
        contract = validate_deepseek_first_live_contract(root)
        validate_deepseek_first_live_implementation(root)
        _validate_dependencies()
        source_integrity = _validate_source_integrity(
            root, expected_source_integrity_commitment_sha256
        )
        now = _clock_now(_clock)
        expiry = _parse_utc(authorization_expires_at_utc)
        if expiry <= now:
            raise _error("deepseek_first_live_authorization_expired", not_run=True)
        if expiry - now < timedelta(seconds=_WHOLE_PROCESS_TIMEOUT_SECONDS):
            raise _error(
                "deepseek_first_live_authorization_window_too_short", not_run=True
            )
        if expiry - now > timedelta(hours=24):
            raise _error(
                "deepseek_first_live_authorization_horizon_exceeded", not_run=True
            )
        resolved_date, resolved_url, input_price, output_price = _pricing(
            now=now,
            snapshot_date=pricing_snapshot_date,
            source_url=pricing_source_url,
            input_price=input_price_per_million_cny,
            output_price=output_price_per_million_cny,
        )
        head, clean, origin_main = _git_state_loader(root)
        if (
            head != expected_execution_commit
            or origin_main != expected_execution_commit
            or not clean
        ):
            raise _error("deepseek_first_live_execution_tree_invalid", not_run=True)
        environment = os.environ
        if not _environment_isolated(environment) or not _network_logging_disabled():
            raise _error("deepseek_first_live_environment_not_isolated", not_run=True)
        if (
            _monotonic_now(_monotonic) - wall_started
            >= _WHOLE_PROCESS_TIMEOUT_SECONDS
        ):
            raise _error("deepseek_first_live_total_timeout", not_run=True)
    except DeepSeekFirstLiveValidationError as exc:
        return _not_run(exc.code)
    except Exception:
        return _not_run("deepseek_first_live_local_gate_failed")

    canonical_expiry = _timestamp(expiry)
    authorization_id_sha256 = _sha256(authorization_id.encode("utf-8"))
    authorization_binding_sha256 = _authorization_binding(
        authorization_id=authorization_id,
        expires_at_utc=canonical_expiry,
        execution_commit=expected_execution_commit,
        source_integrity_commitment=expected_source_integrity_commitment_sha256,
        pricing_snapshot_date=resolved_date.isoformat(),
        pricing_source_url=resolved_url,
        input_price=input_price,
        output_price=output_price,
    )
    directory: Path | None = None
    try:
        directory = _artifact_directory(root, authorization_id_sha256, _artifact_root)
        consumption = {
            "schema_version": CONSUMPTION_SCHEMA_VERSION,
            "status": "consumed",
            "contract_id": VALIDATION_ID,
            "contract_commitment_sha256": CONTRACT_COMMITMENT_SHA256,
            "implementation_commitment_sha256": (
                IMPLEMENTATION_COMMITMENT_SHA256
            ),
            "source_integrity_plan_id": source_integrity["plan_id"],
            "source_integrity_commitment_sha256": (
                expected_source_integrity_commitment_sha256
            ),
            "execution_commit": expected_execution_commit,
            "authorization_id_sha256": authorization_id_sha256,
            "authorization_binding_sha256": authorization_binding_sha256,
            "authorization_expires_at_utc": canonical_expiry,
            "consumed_at_utc": _timestamp(now),
            "pricing_snapshot_date": resolved_date.isoformat(),
            "pricing_source_url": resolved_url,
            "input_price_per_million_cny": _money(input_price),
            "output_price_per_million_cny": _money(output_price),
            "consume_before_key_load": True,
            "provider_key_loaded_at_consumption": False,
            "network_attempts_at_consumption": 0,
            "model_requests_at_consumption": 0,
            "authorizes_retry": False,
            "authorizes_resume": False,
            "authorizes_evaluation": False,
            "authorizes_status_closure": False,
        }
        consumption_bytes, consumption_sha256 = _exclusive_json(
            directory / "consumption.json", consumption
        )
    except DeepSeekFirstLiveValidationError as exc:
        if directory is not None:
            try:
                directory.rmdir()
            except OSError:
                pass
        return _not_run(exc.code)
    except Exception:
        if directory is not None:
            try:
                directory.rmdir()
            except OSError:
                pass
        return _not_run("deepseek_first_live_consumption_write_failed")

    if directory is None:  # pragma: no cover - successful write establishes it
        return _not_run("deepseek_first_live_consumption_write_failed")

    authorization = _ConsumedValidationAuthorization._create(
        _AUTHORITY_TOKEN,
        authorization_id_sha256=authorization_id_sha256,
        authorization_binding_sha256=authorization_binding_sha256,
        authorization_expires_at_utc=expiry,
        contract_commitment_sha256=CONTRACT_COMMITMENT_SHA256,
        implementation_commitment_sha256=IMPLEMENTATION_COMMITMENT_SHA256,
        execution_commit=expected_execution_commit,
        source_integrity_commitment_sha256=(
            expected_source_integrity_commitment_sha256
        ),
        input_price_per_million_cny=input_price,
        output_price_per_million_cny=output_price,
    )
    del consumption_bytes
    status = "failed"
    error_code: str | None = None
    outcome_unknown = False
    key: str | None = None
    key_loaded = False
    network_attempts = 0
    network_call_observation_complete = False
    model_requests = 0
    raw_response_cleanup_count = 0
    ledger: AuditLedger | None = None
    ledger_run_status: str | None = None
    ledger_terminalization_failed = False
    tracker: RuntimeDenominatorTracker | None = None
    runtime_plan_binding: VerifiedRuntimeDenominatorPlanBinding | None = None
    session: _DeepSeekFirstLiveValidationLedgerSession | None = None
    run_id: str | None = None
    audit_index: list[dict[str, Any]] = []
    completion: dict[str, Any] | None = None
    manifest_sha256: str | None = None
    manifest: dict[str, Any] | None = None
    observed_input_tokens: int | None = None
    observed_output_tokens: int | None = None
    observed_cost: Decimal | None = None
    live_input_tokens = 0
    live_output_tokens = 0
    cancelled: asyncio.CancelledError | None = None
    started_at = now
    try:
        remaining = max(0.0, (expiry - datetime.now(timezone.utc)).total_seconds())
        wall_remaining = max(
            0.0,
            _WHOLE_PROCESS_TIMEOUT_SECONDS
            - (_monotonic_now(_monotonic) - wall_started),
        )
        execution_window = max(
            0.0,
            min(wall_remaining, remaining) - _TERMINALIZATION_RESERVE_SECONDS,
        )
        async with asyncio.timeout(execution_window):
            _revalidate_execution_state(
                root,
                expected_source_integrity_commitment=(
                    expected_source_integrity_commitment_sha256
                ),
                expected_execution_commit=expected_execution_commit,
                git_state_loader=_git_state_loader,
            )
            if _key_loader is None:
                raise _error("deepseek_first_live_key_loader_missing")
            try:
                key = _key_loader()
            except Exception:
                raise _error("deepseek_first_live_key_load_failed") from None
            if not isinstance(key, str) or not key.strip():
                raise _error("deepseek_first_live_key_missing")
            key_loaded = True

            tracker, runtime_plan_binding = _mint_validation_tracker(
                root, authorization
            )
            ledger = AuditLedger(directory / "audit.sqlite3")
            run_id = ledger.start_run(
                mode="deepseek_completion_first_live_validation",
                run_id="RUN-DEEPSEEK-FIRST-LIVE-VALIDATION-001",
                request_summary={
                    "validation_id": VALIDATION_ID,
                    "contract_commitment_sha256": CONTRACT_COMMITMENT_SHA256,
                    "implementation_commitment_sha256": (
                        IMPLEMENTATION_COMMITMENT_SHA256
                    ),
                    "authorization_id_sha256": authorization_id_sha256,
                    "scenario_count": 2,
                    "provider": "deepseek",
                    "model": "deepseek-v4-flash",
                },
            )
            session = _DeepSeekFirstLiveValidationLedgerSession(
                tracker.bind_case(VALIDATION_CASE_ID),
                ledger=ledger,
                run_id=run_id,
                runtime_plan_binding=runtime_plan_binding,
                authorization=authorization,
            )
            scenarios = contract["frozen_inputs"]["scenarios"]
            from agents import ModelSettings
            from .model_providers import DeepSeekProvider, ProviderConfigurationError

            try:
                async with asyncio.timeout(_REQUEST_PHASE_TIMEOUT_SECONDS):
                    async with DeepSeekProvider().open_model(
                        model_id="deepseek-v4-flash",
                        api_key=key,
                        timeout_seconds=_REQUEST_TIMEOUT_SECONDS,
                        completion_telemetry_session=session,
                    ) as bound:
                        for scenario_index, scenario in enumerate(scenarios):
                            if model_requests >= 2:
                                raise _error("deepseek_first_live_request_cap_exceeded")
                            if datetime.now(timezone.utc) >= expiry:
                                raise _error(
                                    "deepseek_first_live_authorization_expired_during_run"
                                )
                            _revalidate_execution_state(
                                root,
                                expected_source_integrity_commitment=(
                                    expected_source_integrity_commitment_sha256
                                ),
                                expected_execution_commit=expected_execution_commit,
                                git_state_loader=_git_state_loader,
                            )
                            model_requests += 1
                            try:
                                try:
                                    async with asyncio.timeout(
                                        _REQUEST_TIMEOUT_SECONDS
                                    ):
                                        response = await bound.sdk_model._fetch_response(
                                            None,
                                            scenario["input"],
                                            ModelSettings(
                                                max_tokens=scenario[
                                                    "max_output_tokens"
                                                ]
                                            ),
                                            [],
                                            None,
                                            [],
                                        )
                                except TimeoutError:
                                    raise _error(
                                        "deepseek_first_live_request_timeout",
                                        outcome_unknown=True,
                                    ) from None
                                raw_response_cleanup_count += 1
                                accepted_record = session.accepted_record(scenario_index)
                                if (
                                    accepted_record.get(
                                        "normalized_completion_state"
                                    )
                                    != scenario[
                                        "expected_normalized_completion_state"
                                    ]
                                    or accepted_record.get(
                                        "truncation_signal_source"
                                    )
                                    != "native_status"
                                    or accepted_record.get("record_provenance")
                                    != "live_adapter_write"
                                    or _completion_shape_projection(accepted_record)
                                    != _EXPECTED_COMPLETION_SHAPES[scenario_index]
                                ):
                                    raise _error(
                                        "deepseek_first_live_scenario_shape_mismatch"
                                    )
                                usage = getattr(response, "usage", None)
                                input_count = getattr(usage, "input_tokens", None)
                                output_count = getattr(usage, "output_tokens", None)
                                if (
                                    type(input_count) is not int
                                    or input_count < 0
                                    or type(output_count) is not int
                                    or output_count < 0
                                ):
                                    raise _error("deepseek_first_live_usage_incomplete")
                                live_input_tokens += input_count
                                live_output_tokens += output_count
                                if (
                                    input_count > 512
                                    or output_count > scenario["max_output_tokens"]
                                    or live_input_tokens > 1024
                                    or live_output_tokens > 272
                                ):
                                    raise _error(
                                        "deepseek_first_live_token_cap_exceeded"
                                    )
                                running_cost = (
                                    Decimal(live_input_tokens) * input_price
                                    + Decimal(live_output_tokens) * output_price
                                ) / Decimal(1_000_000)
                                if running_cost > Decimal("1.000000"):
                                    raise _error(
                                        "deepseek_first_live_cost_stop_exceeded"
                                    )
                            except ProviderConfigurationError:
                                raise
                            finally:
                                response = None
            except TimeoutError:
                raise _error(
                    "deepseek_first_live_request_phase_timeout",
                    outcome_unknown=network_attempts > 0,
                ) from None

            network_attempts, network_call_observation_complete = (
                session.transport_observation_snapshot()
            )
            if (
                not network_call_observation_complete
                or network_attempts != 2
                or network_attempts != model_requests
            ):
                raise _error("deepseek_first_live_network_observation_mismatch")

            tracker.seal_case(
                VALIDATION_CASE_ID,
                sdk_raw_response_count=2,
                sdk_usage_request_count=2,
                sdk_request_usage_indices_by_response={0: (0,), 1: (0,)},
            )
            if (
                _monotonic_now(_monotonic) - wall_started
                > _WHOLE_PROCESS_TIMEOUT_SECONDS
            ):
                raise _error("deepseek_first_live_total_timeout", outcome_unknown=False)
            ledger.set_run_status(run_id, "completed")
            ledger_run_status = "completed"
            verification = ledger.verify_chain(run_id)
            audit_index = [
                {
                    "case_id": VALIDATION_CASE_ID,
                    "run_id": run_id,
                    "chain_verification": verification.to_dict(),
                    "completion_telemetry_event_commitment": (
                        session.event_commitment()
                    ),
                }
            ]
            completion = _finalize_runtime_completion_telemetry(
                tracker,
                ledger=ledger,
                audit_index=(
                    {
                        "task_id": VALIDATION_CASE_ID,
                        "run_id": run_id,
                        "chain_verification": verification.to_dict(),
                        "completion_telemetry_event_commitment": (
                            session.event_commitment()
                        ),
                    },
                ),
                ledger_failure_observed=session.failed,
            )
            records = completion["runtime_denominator"]["records"]
            states = [item["normalized_completion_state"] for item in records]
            sources = [item["truncation_signal_source"] for item in records]
            shapes = [_completion_shape_projection(item) for item in records]
            if (
                completion["closure"]["claim_allowed"] is not True
                or states != ["completed", "incomplete_length"]
                or sources != ["native_status", "native_status"]
                or shapes != _EXPECTED_COMPLETION_SHAPES
                or len(records) != 2
                or any(item.get("record_provenance") != "live_adapter_write" for item in records)
                or any(item.get("usage", {}).get("complete") is not True for item in records)
            ):
                raise _error("deepseek_first_live_success_gate_failed")
            input_tokens, output_tokens, observed_cost = _validated_success_usage(
                records,
                input_price=input_price,
                output_price=output_price,
                error_code="deepseek_first_live_token_or_cost_cap_exceeded",
            )
            if (
                input_tokens != live_input_tokens
                or output_tokens != live_output_tokens
            ):
                raise _error("deepseek_first_live_usage_reconciliation_failed")
            observed_input_tokens = input_tokens
            observed_output_tokens = output_tokens
            evidence = {
                "schema_version": EVIDENCE_SCHEMA_VERSION,
                "status": "validated",
                "contract_id": VALIDATION_ID,
                "contract_commitment_sha256": CONTRACT_COMMITMENT_SHA256,
                "implementation_commitment_sha256": (
                    IMPLEMENTATION_COMMITMENT_SHA256
                ),
                "runtime_plan": completion["runtime_plan"],
                "runtime_denominator": completion["runtime_denominator"],
                "ledger_reconciliation": completion["ledger_reconciliation"],
                "validation_integrity_gate_passed": True,
                "observed_states_in_order": states,
                "observed_signal_sources_in_order": sources,
                "observed_completion_shapes_in_order": shapes,
                "observed_input_tokens": input_tokens,
                "observed_output_tokens": output_tokens,
                "local_observed_cost_cny": _money(observed_cost),
                "status_defect_closure_allowed": False,
                "closure_claim_allowed": False,
                "automatic_registry_promotion_allowed": False,
                "raw_response_body_persisted": False,
                "message_content_persisted": False,
                "provider_key_persisted": False,
            }
            completion = evidence
            manifest, manifest_sha256 = _write_evidence_artifacts(
                directory,
                consumption_sha256=consumption_sha256,
                ledger=ledger,
                audit_index=audit_index,
                completion=evidence,
                runtime_plan_binding=runtime_plan_binding,
            )
            status = "success"
    except asyncio.CancelledError as exc:
        cancelled = exc
        error_code = "deepseek_first_live_cancelled"
        outcome_unknown = network_attempts > 0
    except TimeoutError:
        error_code = "deepseek_first_live_total_timeout"
        outcome_unknown = network_attempts > 0
    except DeepSeekFirstLiveValidationError as exc:
        error_code = exc.code
        outcome_unknown = exc.outcome_unknown
    except Exception as exc:
        code = getattr(exc, "code", None)
        error_code = (
            code
            if isinstance(code, str) and _SAFE_ERROR.fullmatch(code)
            else "deepseek_first_live_execution_failed"
        )
        outcome_unknown = network_attempts > 0
    finally:
        key = None

    if session is not None:
        try:
            network_attempts, network_call_observation_complete = (
                session.transport_observation_snapshot()
            )
            outcome_unknown = session.observed_request_outcome_unknown()
        except Exception:
            network_call_observation_complete = False

    if (
        status != "success"
        and ledger is not None
        and run_id is not None
        and ledger_run_status is None
    ):
        target_run_status = "cancelled" if cancelled is not None else "failed"
        try:
            ledger.set_run_status(
                run_id,
                target_run_status,
                terminal_error_code=error_code,
            )
            ledger_run_status = target_run_status
        except Exception:
            error_code = "deepseek_first_live_audit_terminalization_failed"
            ledger_terminalization_failed = True
            manifest = None
            manifest_sha256 = None

    if (
        status != "success"
        and isinstance(completion, Mapping)
        and completion.get("schema_version") == "phase6-completion-telemetry/1.0"
        and runtime_plan_binding is not None
        and ledger is not None
        and not ledger_terminalization_failed
    ):
        try:
            partial_evidence = _failed_evidence(
                completion,
                error_code=error_code,
                outcome_unknown=outcome_unknown,
            )
            completion = partial_evidence
            manifest, manifest_sha256 = _write_evidence_artifacts(
                directory,
                consumption_sha256=consumption_sha256,
                ledger=ledger,
                audit_index=audit_index,
                completion=partial_evidence,
                runtime_plan_binding=runtime_plan_binding,
            )
        except Exception:
            manifest = None
            manifest_sha256 = None
    elif (
        status != "success"
        and tracker is not None
        and runtime_plan_binding is not None
        and ledger is not None
        and run_id is not None
        and completion is None
        and not ledger_terminalization_failed
    ):
        try:
            try:
                tracker.seal_case(
                    VALIDATION_CASE_ID,
                    sdk_raw_response_count=None,
                    sdk_usage_request_count=None,
                    sdk_request_usage_indices_by_response=None,
                )
            except CompletionCaptureError as exc:
                if exc.code != "completion_capture_case_sealed":
                    raise
            verification = ledger.verify_chain(run_id)
            commitment = session.event_commitment() if session is not None else None
            partial_index = [
                {
                    "case_id": VALIDATION_CASE_ID,
                    "run_id": run_id,
                    "chain_verification": verification.to_dict(),
                    "completion_telemetry_event_commitment": commitment,
                }
            ]
            outer = _finalize_runtime_completion_telemetry(
                tracker,
                ledger=ledger,
                audit_index=(
                    {
                        "task_id": VALIDATION_CASE_ID,
                        "run_id": run_id,
                        "chain_verification": verification.to_dict(),
                        "completion_telemetry_event_commitment": commitment,
                    },
                ),
                ledger_failure_observed=(
                    session.failed if session is not None else True
                ),
            )
            partial_evidence = _failed_evidence(
                outer,
                error_code=error_code,
                outcome_unknown=outcome_unknown,
            )
            completion = partial_evidence
            manifest, manifest_sha256 = _write_evidence_artifacts(
                directory,
                consumption_sha256=consumption_sha256,
                ledger=ledger,
                audit_index=partial_index,
                completion=partial_evidence,
                runtime_plan_binding=runtime_plan_binding,
            )
        except Exception:
            manifest = None
            manifest_sha256 = None

    if completion is not None and observed_input_tokens is None:
        denominator = completion.get("runtime_denominator")
        records = denominator.get("records") if isinstance(denominator, Mapping) else None
        totals = _usage_totals(records)
        if totals is not None:
            observed_input_tokens, observed_output_tokens = totals
            observed_cost = (
                Decimal(observed_input_tokens) * input_price
                + Decimal(observed_output_tokens) * output_price
            ) / Decimal(1_000_000)

    completed_at = datetime.now(timezone.utc)
    partial_artifacts = (
        {} if manifest is not None else _partial_artifact_snapshot(directory)
    )
    terminal = {
        "schema_version": TERMINAL_SCHEMA_VERSION,
        "status": status,
        "error_code": error_code,
        "outcome_unknown": outcome_unknown,
        "contract_id": VALIDATION_ID,
        "contract_commitment_sha256": CONTRACT_COMMITMENT_SHA256,
        "implementation_commitment_sha256": IMPLEMENTATION_COMMITMENT_SHA256,
        "source_integrity_plan_id": source_integrity["plan_id"],
        "source_integrity_commitment_sha256": (
            expected_source_integrity_commitment_sha256
        ),
        "execution_commit": expected_execution_commit,
        "authorization_id_sha256": authorization_id_sha256,
        "authorization_binding_sha256": authorization_binding_sha256,
        "authorization_expires_at_utc": canonical_expiry,
        "consumption_receipt_sha256": consumption_sha256,
        "started_at_utc": _timestamp(started_at),
        "completed_at_utc": _timestamp(completed_at),
        "network_attempts": network_attempts,
        "network_attempt_limit": 2,
        "network_calls": (
            network_attempts if network_call_observation_complete else None
        ),
        "network_call_observation_complete": network_call_observation_complete,
        "model_requests": model_requests,
        "model_request_limit": 2,
        "input_tokens": observed_input_tokens,
        "input_token_limit": 1024,
        "output_tokens": observed_output_tokens,
        "output_token_limit": 272,
        "usage_complete": (
            observed_input_tokens is not None and observed_output_tokens is not None
        ),
        "local_observed_usage_cost_cny": (
            _money(observed_cost) if observed_cost is not None else None
        ),
        "local_observed_usage_cost_stop_cny": "1.000000",
        "strict_provider_billing_hard_cap": False,
        "actual_provider_billed_cost_cny": None,
        "manifest_sha256": manifest_sha256,
        "manifest_complete": manifest is not None,
        "partial_artifacts": partial_artifacts,
        "ledger_run_status": ledger_run_status,
        "provider_key_loaded": key_loaded,
        "provider_key_persisted": False,
        "raw_response_body_persisted": False,
        "raw_response_cleanup_complete": (
            model_requests > 0 and raw_response_cleanup_count == model_requests
        ),
        "message_content_persisted": False,
        "input_content_repeated_in_receipt": False,
        "exception_text_persisted": False,
        "closure_claim_allowed": False,
        "automatic_registry_promotion_allowed": False,
        "authorizes_retry": False,
        "authorizes_resume": False,
        "authorizes_evaluation": False,
        "authorizes_provider_registration": False,
        "authorizes_model_quality_claim": False,
    }
    try:
        _exclusive_json(directory / "terminal.json", terminal)
    except Exception as exc:
        if cancelled is not None:
            raise cancelled
        raise _error(
            "deepseek_first_live_terminal_write_failed",
            outcome_unknown=outcome_unknown,
        ) from exc
    if cancelled is not None:
        raise cancelled
    return terminal


async def run_deepseek_first_live_validation(
    *,
    project_root: str | Path,
    authorization_id: str | None,
    authorization_expires_at_utc: str | None,
    expected_contract_commitment_sha256: str | None,
    expected_source_integrity_commitment_sha256: str | None,
    expected_execution_commit: str | None,
    pricing_snapshot_date: str | None,
    pricing_source_url: str | None,
    input_price_per_million_cny: str | Decimal | None,
    output_price_per_million_cny: str | Decimal | None,
    confirm_online: bool,
    accept_locked_caps: bool,
    attest_pricing_current: bool,
) -> dict[str, Any]:
    """Supported production entrypoint; test seams are structurally unavailable."""

    return await _run_deepseek_first_live_validation_impl(
        project_root=project_root,
        authorization_id=authorization_id,
        authorization_expires_at_utc=authorization_expires_at_utc,
        expected_contract_commitment_sha256=(
            expected_contract_commitment_sha256
        ),
        expected_source_integrity_commitment_sha256=(
            expected_source_integrity_commitment_sha256
        ),
        expected_execution_commit=expected_execution_commit,
        pricing_snapshot_date=pricing_snapshot_date,
        pricing_source_url=pricing_source_url,
        input_price_per_million_cny=input_price_per_million_cny,
        output_price_per_million_cny=output_price_per_million_cny,
        confirm_online=confirm_online,
        accept_locked_caps=accept_locked_caps,
        attest_pricing_current=attest_pricing_current,
        _key_loader=lambda: os.environ.get("DEEPSEEK_API_KEY"),
        _clock=lambda: datetime.now(timezone.utc),
        _git_state_loader=_git_state,
        _artifact_root=None,
        _monotonic=time.monotonic,
    )


__all__ = [
    "CONTRACT_COMMITMENT_SHA256",
    "CONTRACT_RELATIVE_PATH",
    "IMPLEMENTATION_COMMITMENT_SHA256",
    "IMPLEMENTATION_RELATIVE_PATH",
    "DeepSeekFirstLiveValidationError",
    "deepseek_first_live_validation_status",
    "run_deepseek_first_live_validation",
    "validate_deepseek_first_live_contract",
    "validate_deepseek_first_live_implementation",
    "verify_deepseek_first_live_artifacts",
]
