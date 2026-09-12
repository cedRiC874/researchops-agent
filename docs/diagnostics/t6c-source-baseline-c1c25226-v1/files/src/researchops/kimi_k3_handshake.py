from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Awaitable, Callable, Final, Mapping

from .kimi_chat_nonstreaming import (
    KIMI_NONSTREAMING_TRANSPORT_ID,
    KimiNonstreamingResponse,
    OFFICIAL_SOURCE_COMMITMENTS,
    kimi_nonstreaming_contract,
    request_body_bytes,
    run_kimi_nonstreaming_completion,
)
from .kimi_chat_transport import (
    KIMI_INVALID_REQUEST_PROBE_BODY_SHA256,
    KimiAssistantMessage,
    KimiChatRequest,
    KimiChatTransportError,
    KimiFunctionTool,
    KimiInvalidRequestProbeResult,
    KimiTextMessage,
    KimiToolResultMessage,
    run_kimi_invalid_request_probe,
)
from .kimi_synthetic_tools import (
    METRIC_ID,
    SUCCESS_DATASET_ID,
    TOOL_NAME,
    KimiSyntheticToolError,
    KimiSyntheticToolExecutor,
    synthetic_tool_schema,
)


HANDSHAKE_ID: Final = "kimi-k3-controlled-synthetic-handshake-v1"
PLAN_SCHEMA_VERSION: Final = "kimi-k3-handshake-plan/1.0"
CONTRACT_SCHEMA_VERSION: Final = "kimi-k3-handshake-contract/1.0"
CONSUMPTION_SCHEMA_VERSION: Final = "kimi-k3-handshake-consumption/1.0"
TERMINAL_SCHEMA_VERSION: Final = "kimi-k3-handshake-terminal/1.0"
PLAN_RELATIVE_PATH: Final = Path("evals/v2/kimi_k3_handshake_plan_v1.json")
CONTRACT_RELATIVE_PATH: Final = Path("evals/v2/kimi_k3_handshake_contract_v1.json")
TRANSPORT_CONTRACT_RELATIVE_PATH: Final = Path(
    "evals/v2/kimi_k3_nonstreaming_contract.json"
)
ARTIFACT_RELATIVE_PATH: Final = Path("artifacts/kimi_k3_handshake_v1")
PLAN_COMMITMENT_SHA256: Final[str]
PREDECESSOR_CANDIDATE_COMMITMENT_SHA256: Final = (
    "b41269ac6db96e2999fedc95f08f3b77a48699f8c0b50b63764bcb6e1f9e962c"
)

NETWORK_CALL_LIMIT: Final = 3
MODEL_REQUEST_LIMIT: Final = 2
INPUT_TOKEN_LIMIT_PER_REQUEST: Final = 8_000
INPUT_TOKEN_LIMIT_TOTAL: Final = 16_000
OUTPUT_TOKEN_LIMIT_PER_REQUEST: Final = 1_536
OUTPUT_TOKEN_LIMIT_TOTAL: Final = 3_072
TOOL_EXECUTION_LIMIT: Final = 1
RUN_TIMEOUT_SECONDS: Final = 300
CLIENT_RETRY_LIMIT: Final = 0
LOCAL_COST_LIMIT_CNY: Final = Decimal("2.000000")
INPUT_PRICE_PER_MILLION_CNY: Final = Decimal("20.000000")
OUTPUT_PRICE_PER_MILLION_CNY: Final = Decimal("100.000000")
MAX_AUTHORIZATION_HORIZON: Final = timedelta(hours=24)
TERMS_REVIEWED_AT_UTC: Final = "2026-08-31T14:03:47.639Z"
TERMS_EFFECTIVE_DATE: Final = "2026-08-31"

_AUTHORIZATION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,127}$")
_IMPLEMENTATION_COMPONENT_PATHS: Final = {
    "nonstreaming_transport_source_sha256": Path(
        "src/researchops/kimi_chat_nonstreaming.py"
    ),
    "handshake_runner_source_sha256": Path(
        "src/researchops/kimi_k3_handshake.py"
    ),
    "handshake_cli_source_sha256": Path(
        "src/researchops/kimi_k3_handshake_cli.py"
    ),
    "shared_transport_source_sha256": Path(
        "src/researchops/kimi_chat_transport.py"
    ),
    "usage_contract_source_sha256": Path(
        "src/researchops/kimi_chat_transport_v3.py"
    ),
    "synthetic_tool_source_sha256": Path(
        "src/researchops/kimi_synthetic_tools.py"
    ),
    "requirements_lock_sha256": Path("requirements.lock"),
    "pyproject_sha256": Path("pyproject.toml"),
}


class KimiK3HandshakeError(RuntimeError):
    """Stable, payload-independent handshake failure."""

    def __init__(self, code: str, *, outcome_unknown: bool = False) -> None:
        super().__init__(code)
        self.code = code
        self.outcome_unknown = outcome_unknown


@dataclass(slots=True)
class _Counters:
    network_attempts: int = 0
    network_calls: int = 0
    model_requests: int = 0
    tool_executions: int = 0
    invalid_request_probes: int = 0
    invalid_request_probe_attempted: bool = False
    invalid_request_probe_semantics_verified: bool = False
    invalid_request_probe_http_status: int | None = None
    invalid_request_probe_provider_error_type: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    usage_observed_model_requests: int = 0
    cached_input_tokens_observed: int = 0
    cached_input_usage_observed_model_requests: int = 0
    first_latency_ms: int | None = None
    second_latency_ms: int | None = None
    probe_latency_ms: int | None = None


CompletionRunner = Callable[..., Awaitable[KimiNonstreamingResponse]]
ProbeRunner = Callable[..., Awaitable[KimiInvalidRequestProbeResult]]


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _project_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _implementation_component_hashes(
    project_root: str | Path,
) -> dict[str, str]:
    root = Path(project_root).resolve()
    hashes: dict[str, str] = {}
    for component_id, relative_path in _IMPLEMENTATION_COMPONENT_PATHS.items():
        source = root / relative_path
        if source.is_symlink() or not source.is_file():
            raise KimiK3HandshakeError(
                "kimi_k3_handshake_component_missing_or_unsafe"
            )
        hashes[component_id] = _sha256_file(source)
    return hashes


def _on_disk_plan_commitment() -> str:
    """Expose the checked-in commitment without creating a source self-hash loop."""

    try:
        value = json.loads(
            (_project_root() / PLAN_RELATIVE_PATH).read_text(encoding="utf-8"),
            parse_constant=lambda item: (_ for _ in ()).throw(
                ValueError(f"non-finite value: {item}")
            ),
        )
        if not isinstance(value, dict):
            raise ValueError("plan root is not an object")
        return _sha256(_canonical_bytes(value))
    except (OSError, ValueError, json.JSONDecodeError):
        # Import remains safe enough for the offline validator to emit a stable
        # invalid result.  This sentinel can never satisfy a real authorization.
        return "0" * 64


PLAN_COMMITMENT_SHA256 = _on_disk_plan_commitment()


def _official_sources() -> list[dict[str, str | int]]:
    return [source.to_dict() for source in OFFICIAL_SOURCE_COMMITMENTS]


def kimi_k3_handshake_plan(
    project_root: str | Path | None = None,
) -> dict[str, Any]:
    """Return the immutable one-scenario online plan, excluding authorization."""

    root = _project_root() if project_root is None else Path(project_root).resolve()
    return {
        "schema_version": PLAN_SCHEMA_VERSION,
        "handshake_id": HANDSHAKE_ID,
        "status": "frozen_not_run",
        "purpose": "tool_usage_and_invalid_request_semantics_only",
        "provider_id": "moonshot_kimi",
        "model_id": "kimi-k3",
        "api_origin": "https://api.moonshot.cn",
        "transport_id": KIMI_NONSTREAMING_TRANSPORT_ID,
        "predecessor_candidate_commitment_sha256": (
            PREDECESSOR_CANDIDATE_COMMITMENT_SHA256
        ),
        "scenario": {
            "scenario_id": "kimi_k3_synthetic_success_handshake_v1",
            "dataset_id": SUCCESS_DATASET_ID,
            "metric_id": METRIC_ID,
            "tool_name": TOOL_NAME,
            "expected_value": "0.375",
            "synthetic_only": True,
        },
        "sequence": [
            {
                "step": 1,
                "kind": "model_request",
                "stream": False,
                "tool_choice": "required",
                "expected_finish_reason": "tool_calls",
                "expected_tool_call_count": 1,
            },
            {
                "step": 2,
                "kind": "local_tool_execution",
                "tool_name": TOOL_NAME,
                "expected_execution_count": 1,
            },
            {
                "step": 3,
                "kind": "model_request",
                "stream": False,
                "tool_choice": "none",
                "complete_assistant_message_replay_required": True,
                "tool_result_replay_required": True,
                "expected_finish_reason": "stop",
            },
            {
                "step": 4,
                "kind": "fixed_invalid_request_probe",
                "body_sha256": KIMI_INVALID_REQUEST_PROBE_BODY_SHA256,
                "expected_http_status": 400,
                "expected_provider_error_type": "invalid_request_error",
            },
        ],
        "locked_caps": {
            "network_attempts": NETWORK_CALL_LIMIT,
            "network_calls": NETWORK_CALL_LIMIT,
            "model_requests": MODEL_REQUEST_LIMIT,
            "concurrency": 1,
            "client_retries": CLIENT_RETRY_LIMIT,
            "input_tokens_per_model_request": INPUT_TOKEN_LIMIT_PER_REQUEST,
            "input_tokens_total": INPUT_TOKEN_LIMIT_TOTAL,
            "output_tokens_per_model_request": OUTPUT_TOKEN_LIMIT_PER_REQUEST,
            "output_tokens_total": OUTPUT_TOKEN_LIMIT_TOTAL,
            "tool_executions": TOOL_EXECUTION_LIMIT,
            "total_timeout_seconds": RUN_TIMEOUT_SECONDS,
            "local_observed_usage_cost_stop_cny": "2.000000",
            "strict_provider_billing_hard_cap": False,
            "single_response_overshoot_possible": True,
            "fallback": False,
            "resume": False,
        },
        "pricing": {
            "currency": "CNY",
            "billing_unit_tokens": 1_000_000,
            "uncached_input_per_million": "20.000000",
            "output_per_million": "100.000000",
            "actual_provider_bill_claimed": False,
        },
        "terms_review": {
            "reviewed_at_utc": TERMS_REVIEWED_AT_UTC,
            "displayed_effective_date": TERMS_EFFECTIVE_DATE,
            "official_source_commitments": _official_sources(),
            "provider_service_optimization_use_disclosed": True,
            "synthetic_only_required": True,
            "runtime_no_material_delta_attestation_required": True,
        },
        "authorization": {
            "single_use": True,
            "authorization_id_required": True,
            "authorization_expiry_utc_required": True,
            "expected_plan_commitment_required": True,
            "maximum_future_horizon_hours": 24,
            "consume_before_key_load": True,
            "absolute_deadline_enforced": True,
            "network_attempt_preflight_expiry_check": True,
        },
        "persistence": {
            "consumption_receipt_required": True,
            "terminal_receipt_attempted_on_handled_exit": True,
            "process_crash_or_unrecoverable_io_may_leave_orphan": True,
            "authorization_id_persisted": False,
            "api_key_persisted": False,
            "raw_prompt_output_reasoning_tool_arguments_or_result_persisted": False,
        },
        "component_hashes": _implementation_component_hashes(root),
        "claims": {
            "model_quality": False,
            "provider_registration": False,
            "private_evaluation": False,
            "non_synthetic_data": False,
            "prompt_or_candidate_tuning": False,
        },
    }


def kimi_k3_handshake_contract() -> dict[str, Any]:
    return {
        "schema_version": CONTRACT_SCHEMA_VERSION,
        "contract_id": HANDSHAKE_ID,
        "status": "implemented_offline_tested_not_run",
        "plan_path": PLAN_RELATIVE_PATH.as_posix(),
        "plan_commitment_sha256": PLAN_COMMITMENT_SHA256,
        "transport_contract_path": TRANSPORT_CONTRACT_RELATIVE_PATH.as_posix(),
        "transport_contract": kimi_nonstreaming_contract(),
        "artifact_subdirectory": ARTIFACT_RELATIVE_PATH.as_posix(),
        "single_use_authorization": True,
        "consume_before_key_load": True,
        "absolute_authorization_deadline_enforced": True,
        "terminal_receipt_on_handled_exit": "best_effort_required_attempt",
        "process_crash_or_unrecoverable_io_may_leave_orphan": True,
        "cli_api_key_argument_allowed": False,
        "online_calls_per_success": 3,
        "model_calls_per_success": 2,
        "tool_executions_per_success": 1,
        "actual_provider_bill_claimed": False,
        "model_quality_claim_allowed": False,
        "provider_registration_allowed": False,
        "private_or_non_synthetic_allowed": False,
    }


def _load_json(path: Path) -> dict[str, Any]:
    def no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate JSON key")
            result[key] = value
        return result

    def no_constant(value: str) -> None:
        del value
        raise ValueError("non-finite JSON number")

    value = json.loads(
        path.read_text(encoding="utf-8"),
        object_pairs_hook=no_duplicates,
        parse_constant=no_constant,
    )
    if not isinstance(value, dict):
        raise ValueError("JSON root must be an object")
    return value


def validate_kimi_k3_handshake(project_root: str | Path) -> dict[str, Any]:
    """Validate all frozen inputs without reading environment or using network."""

    root = Path(project_root).resolve()
    plan_path = root / PLAN_RELATIVE_PATH
    contract_path = root / CONTRACT_RELATIVE_PATH
    transport_path = root / TRANSPORT_CONTRACT_RELATIVE_PATH
    plan = _load_json(plan_path)
    contract = _load_json(contract_path)
    transport = _load_json(transport_path)
    expected_plan = kimi_k3_handshake_plan(root)
    expected_contract = kimi_k3_handshake_contract()
    expected_transport = kimi_nonstreaming_contract()
    if plan != expected_plan:
        raise KimiK3HandshakeError("kimi_k3_handshake_plan_drift")
    commitment = _sha256(_canonical_bytes(plan))
    if commitment != PLAN_COMMITMENT_SHA256:
        raise KimiK3HandshakeError("kimi_k3_handshake_plan_commitment_mismatch")
    if contract != expected_contract:
        raise KimiK3HandshakeError("kimi_k3_handshake_contract_drift")
    if transport != expected_transport:
        raise KimiK3HandshakeError("kimi_k3_handshake_transport_contract_drift")
    return {
        "schema_version": "kimi-k3-handshake-validation/1.0",
        "status": "valid",
        "handshake_id": HANDSHAKE_ID,
        "plan_commitment_sha256": commitment,
        "contract_sha256": _sha256(_canonical_bytes(contract)),
        "transport_contract_sha256": _sha256(_canonical_bytes(transport)),
        "network_calls": 0,
        "network_attempts": 0,
        "model_calls": 0,
        "key_loaded": False,
        "online_authorized": False,
    }


def _parse_expiry(value: str) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise KimiK3HandshakeError("kimi_k3_handshake_authorization_expiry_invalid")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise KimiK3HandshakeError(
            "kimi_k3_handshake_authorization_expiry_invalid"
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        raise KimiK3HandshakeError("kimi_k3_handshake_authorization_expiry_invalid")
    return parsed.astimezone(timezone.utc)


def _timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def _clock_now(clock: Callable[[], datetime]) -> datetime:
    value = clock()
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise KimiK3HandshakeError("kimi_k3_handshake_clock_invalid")
    return value.astimezone(timezone.utc)


def _authorization_remaining_seconds(
    expiry: datetime, clock: Callable[[], datetime]
) -> float:
    remaining = (expiry - _clock_now(clock)).total_seconds()
    if remaining <= 0:
        raise KimiK3HandshakeError(
            "kimi_k3_handshake_authorization_expired_during_run"
        )
    return remaining


def _authorization_hash(authorization_id: str) -> str:
    return _sha256(authorization_id.encode("utf-8"))


def _authorization_binding(
    *, authorization_id: str, expires_at_utc: str, plan_commitment: str
) -> str:
    return _sha256(
        _canonical_bytes(
            {
                "authorization_id": authorization_id,
                "authorization_expires_at_utc": expires_at_utc,
                "handshake_id": HANDSHAKE_ID,
                "expected_plan_commitment_sha256": plan_commitment,
                "locked_caps": kimi_k3_handshake_plan()["locked_caps"],
            }
        )
    )


def _artifact_paths(
    project_root: Path,
    authorization_id_sha256: str,
    artifact_directory: Path | None,
) -> tuple[Path, Path]:
    directory = (
        artifact_directory.resolve()
        if artifact_directory is not None
        else (project_root / ARTIFACT_RELATIVE_PATH).resolve()
    )
    return (
        directory / f"{authorization_id_sha256}.consumption.json",
        directory / f"{authorization_id_sha256}.terminal.json",
    )


def _exclusive_write(path: Path, payload: Mapping[str, Any]) -> bytes:
    path.parent.mkdir(parents=True, exist_ok=True)
    body = _canonical_bytes(payload) + b"\n"
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
    return body


def _atomic_terminal_write(path: Path, payload: Mapping[str, Any]) -> bytes:
    try:
        return _exclusive_write(path, payload)
    except FileExistsError as exc:
        raise KimiK3HandshakeError(
            "kimi_k3_handshake_terminal_already_exists"
        ) from exc


def _tool() -> KimiFunctionTool:
    schema = synthetic_tool_schema()["function"]
    return KimiFunctionTool.from_schema(
        name=schema["name"],
        description=schema["description"],
        parameters=schema["parameters"],
    )


def _first_request() -> KimiChatRequest:
    return KimiChatRequest(
        messages=(
            KimiTextMessage(
                "system",
                "Use exactly the declared tool and only the synthetic fixture.",
            ),
            KimiTextMessage(
                "user",
                "Look up effect_size for kimi_synth_success_v1 with the tool.",
            ),
        ),
        tools=(_tool(),),
        tool_choice="required",
        max_completion_tokens=OUTPUT_TOKEN_LIMIT_PER_REQUEST,
        reasoning_effort="low",
    )


def _canonical_tool_result(value: Mapping[str, Any]) -> str:
    return _canonical_bytes(value).decode("utf-8")


def _second_request(
    first: KimiChatRequest,
    assistant: KimiAssistantMessage,
    tool_result: Mapping[str, Any],
) -> KimiChatRequest:
    call = assistant.tool_calls[0]
    return KimiChatRequest(
        messages=(
            *first.messages,
            assistant,
            KimiToolResultMessage(
                call.call_id,
                call.name,
                _canonical_tool_result(tool_result),
            ),
        ),
        tools=first.tools,
        tool_choice="none",
        max_completion_tokens=OUTPUT_TOKEN_LIMIT_PER_REQUEST,
        reasoning_effort="low",
    )


def _record_response(
    counters: _Counters, response: KimiNonstreamingResponse, *, request_number: int
) -> None:
    counters.network_calls += response.network_calls
    _record_model_usage(counters, response.usage)
    if counters.network_calls > NETWORK_CALL_LIMIT:
        raise KimiK3HandshakeError("kimi_k3_handshake_network_cap_exceeded")
    if request_number == 1:
        counters.first_latency_ms = response.latency_ms
    else:
        counters.second_latency_ms = response.latency_ms


def _record_model_usage(counters: _Counters, usage: Any) -> None:
    counters.input_tokens += usage.prompt_tokens
    counters.output_tokens += usage.completion_tokens
    counters.usage_observed_model_requests += 1
    if usage.cached_tokens_reported:
        counters.cached_input_tokens_observed += usage.cached_tokens
        counters.cached_input_usage_observed_model_requests += 1
    if usage.prompt_tokens > INPUT_TOKEN_LIMIT_PER_REQUEST:
        raise KimiK3HandshakeError("kimi_k3_handshake_input_token_cap_exceeded")
    if usage.completion_tokens > OUTPUT_TOKEN_LIMIT_PER_REQUEST:
        raise KimiK3HandshakeError("kimi_k3_handshake_output_token_cap_exceeded")
    if counters.input_tokens > INPUT_TOKEN_LIMIT_TOTAL:
        raise KimiK3HandshakeError("kimi_k3_handshake_input_token_cap_exceeded")
    if counters.output_tokens > OUTPUT_TOKEN_LIMIT_TOTAL:
        raise KimiK3HandshakeError("kimi_k3_handshake_output_token_cap_exceeded")
    if _local_cost(counters.input_tokens, counters.output_tokens) > LOCAL_COST_LIMIT_CNY:
        raise KimiK3HandshakeError("kimi_k3_handshake_local_cost_stop_exceeded")


def _reserve_network_attempt(counters: _Counters) -> None:
    if counters.network_attempts >= NETWORK_CALL_LIMIT:
        raise KimiK3HandshakeError("kimi_k3_handshake_network_cap_exceeded")
    counters.network_attempts += 1


def _local_cost(input_tokens: int, output_tokens: int) -> Decimal:
    return (
        Decimal(input_tokens) * INPUT_PRICE_PER_MILLION_CNY
        + Decimal(output_tokens) * OUTPUT_PRICE_PER_MILLION_CNY
    ) / Decimal(1_000_000)


def _money(value: Decimal) -> str:
    return format(value.quantize(Decimal("0.000001")), "f")


def _terminal_receipt(
    *,
    status: str,
    error_code: str | None,
    outcome_unknown: bool,
    authorization_id_sha256: str,
    authorization_binding_sha256: str,
    authorization_expires_at_utc: str,
    consumption_receipt_sha256: str,
    started_at: datetime,
    completed_at: datetime,
    counters: _Counters,
) -> dict[str, Any]:
    local_cost = _local_cost(counters.input_tokens, counters.output_tokens)
    model_usage_complete = (
        counters.model_requests > 0
        and counters.usage_observed_model_requests == counters.model_requests
    )
    probe_coverage_complete = (
        not counters.invalid_request_probe_attempted
        or counters.invalid_request_probe_semantics_verified
    )
    usage_complete = model_usage_complete and probe_coverage_complete
    return {
        "schema_version": TERMINAL_SCHEMA_VERSION,
        "status": status,
        "error_code": error_code,
        "outcome_unknown": outcome_unknown,
        "handshake_id": HANDSHAKE_ID,
        "plan_commitment_sha256": PLAN_COMMITMENT_SHA256,
        "authorization_id_sha256": authorization_id_sha256,
        "authorization_binding_sha256": authorization_binding_sha256,
        "authorization_expires_at_utc": authorization_expires_at_utc,
        "consumption_receipt_sha256": consumption_receipt_sha256,
        "started_at_utc": _timestamp(started_at),
        "completed_at_utc": _timestamp(completed_at),
        "scenario_count": 1,
        "scenarios_completed": 1 if status == "success" else 0,
        "network_attempts": counters.network_attempts,
        "network_attempt_limit": NETWORK_CALL_LIMIT,
        "network_calls": counters.network_calls,
        "network_call_limit": NETWORK_CALL_LIMIT,
        "network_call_observation_complete": (
            counters.network_attempts == counters.network_calls
        ),
        "model_requests": counters.model_requests,
        "model_request_limit": MODEL_REQUEST_LIMIT,
        "tool_executions": counters.tool_executions,
        "tool_execution_limit": TOOL_EXECUTION_LIMIT,
        "invalid_request_probes": counters.invalid_request_probes,
        "invalid_request_probe_expected": 1,
        "invalid_request_probe_attempted": (
            counters.invalid_request_probe_attempted
        ),
        "invalid_request_probe_semantics_verified": (
            counters.invalid_request_probe_semantics_verified
        ),
        "invalid_request_probe_http_status": (
            counters.invalid_request_probe_http_status
        ),
        "invalid_request_probe_provider_error_type": (
            counters.invalid_request_probe_provider_error_type
        ),
        "input_tokens": counters.input_tokens,
        "input_token_limit": INPUT_TOKEN_LIMIT_TOTAL,
        "output_tokens": counters.output_tokens,
        "output_token_limit": OUTPUT_TOKEN_LIMIT_TOTAL,
        "usage_observed_model_requests": counters.usage_observed_model_requests,
        "model_request_usage_complete": model_usage_complete,
        "usage_complete": usage_complete,
        "usage_coverage_scope": (
            "model_requests_and_verified_invalid_request_probe_semantics"
        ),
        "cached_input_tokens": (
            counters.cached_input_tokens_observed
            if counters.model_requests > 0
            and counters.cached_input_usage_observed_model_requests
            == counters.model_requests
            else None
        ),
        "cached_input_tokens_observed": counters.cached_input_tokens_observed,
        "cached_input_usage_observed_model_requests": (
            counters.cached_input_usage_observed_model_requests
        ),
        "cached_input_usage_complete": (
            counters.model_requests > 0
            and counters.cached_input_usage_observed_model_requests
            == counters.model_requests
        ),
        "latency_ms": {
            "first_model_request": counters.first_latency_ms,
            "second_model_request": counters.second_latency_ms,
            "invalid_request_probe": counters.probe_latency_ms,
        },
        "local_observed_usage_cost_cny": _money(local_cost),
        "local_observed_usage_cost_stop_cny": _money(LOCAL_COST_LIMIT_CNY),
        "local_observed_usage_cost_coverage": (
            "complete" if usage_complete else "incomplete"
        ),
        "strict_provider_billing_hard_cap": False,
        "input_price_per_million_cny": _money(INPUT_PRICE_PER_MILLION_CNY),
        "output_price_per_million_cny": _money(OUTPUT_PRICE_PER_MILLION_CNY),
        "actual_provider_billed_cost_cny": None,
        "raw_prompt_output_reasoning_tool_arguments_or_result_persisted": False,
        "api_key_persisted": False,
        "authorizes_retry": False,
        "authorizes_resume": False,
        "authorizes_prompt_or_candidate_tuning": False,
        "authorizes_model_quality_claim": False,
        "authorizes_provider_registration": False,
        "authorizes_private_evaluation": False,
        "authorizes_non_synthetic_data": False,
    }


def _not_run_receipt(error_code: str) -> dict[str, Any]:
    return {
        "schema_version": "kimi-k3-handshake-cli-gate/1.0",
        "status": "not_run",
        "error_code": error_code,
        "handshake_id": HANDSHAKE_ID,
        "plan_commitment_sha256": PLAN_COMMITMENT_SHA256,
        "network_attempts": 0,
        "network_calls": 0,
        "model_requests": 0,
        "tool_executions": 0,
        "key_loaded": False,
        "authorization_consumed": False,
        "authorizes_retry": False,
        "authorizes_resume": False,
        "authorizes_model_quality_claim": False,
        "authorizes_provider_registration": False,
        "authorizes_private_evaluation": False,
        "authorizes_non_synthetic_data": False,
    }


async def run_kimi_k3_handshake(
    *,
    project_root: str | Path,
    authorization_id: str | None,
    authorization_expires_at_utc: str | None,
    expected_plan_commitment_sha256: str | None,
    confirm_online: bool,
    accept_locked_caps: bool,
    attest_terms_and_pricing_unchanged: bool,
    _key_loader: Callable[[], str | None] | None = None,
    _completion_runner: CompletionRunner = run_kimi_nonstreaming_completion,
    _probe_runner: ProbeRunner = run_kimi_invalid_request_probe,
    _clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    _artifact_directory: Path | None = None,
) -> dict[str, Any]:
    """Consume one authorization and perform the frozen synthetic handshake.

    The public function deliberately has no API-Key parameter.  ``_key_loader``
    is a private dependency-injection seam used by the CLI and offline tests.
    """

    if confirm_online is not True:
        return _not_run_receipt("kimi_k3_handshake_confirmation_required")
    if accept_locked_caps is not True:
        return _not_run_receipt("kimi_k3_handshake_locked_caps_not_accepted")
    if attest_terms_and_pricing_unchanged is not True:
        return _not_run_receipt(
            "kimi_k3_handshake_terms_and_pricing_attestation_required"
        )
    if (
        not isinstance(expected_plan_commitment_sha256, str)
        or expected_plan_commitment_sha256 != PLAN_COMMITMENT_SHA256
    ):
        return _not_run_receipt("kimi_k3_handshake_commitment_not_authorized")
    if (
        not isinstance(authorization_id, str)
        or _AUTHORIZATION_ID.fullmatch(authorization_id) is None
    ):
        return _not_run_receipt("kimi_k3_handshake_authorization_id_invalid")
    if not isinstance(authorization_expires_at_utc, str):
        return _not_run_receipt("kimi_k3_handshake_authorization_expiry_invalid")

    root = Path(project_root).resolve()
    try:
        validation = validate_kimi_k3_handshake(root)
        expiry = _parse_expiry(authorization_expires_at_utc)
        canonical_expiry = _timestamp(expiry)
        started_at = _clock_now(_clock)
    except (OSError, ValueError, json.JSONDecodeError, KimiK3HandshakeError) as exc:
        code = (
            exc.code
            if isinstance(exc, KimiK3HandshakeError)
            else "kimi_k3_handshake_local_contract_invalid"
        )
        return _not_run_receipt(code)
    if expiry <= started_at:
        return _not_run_receipt("kimi_k3_handshake_authorization_expired")
    if expiry - started_at < timedelta(seconds=RUN_TIMEOUT_SECONDS):
        return _not_run_receipt(
            "kimi_k3_handshake_authorization_window_too_short"
        )
    if expiry - started_at > MAX_AUTHORIZATION_HORIZON:
        return _not_run_receipt("kimi_k3_handshake_authorization_horizon_exceeded")
    if validation["plan_commitment_sha256"] != expected_plan_commitment_sha256:
        return _not_run_receipt("kimi_k3_handshake_commitment_not_authorized")

    authorization_id_sha256 = _authorization_hash(authorization_id)
    binding_sha256 = _authorization_binding(
        authorization_id=authorization_id,
        expires_at_utc=canonical_expiry,
        plan_commitment=expected_plan_commitment_sha256,
    )
    consumption_path, terminal_path = _artifact_paths(
        root, authorization_id_sha256, _artifact_directory
    )
    consumption = {
        "schema_version": CONSUMPTION_SCHEMA_VERSION,
        "status": "consumed",
        "handshake_id": HANDSHAKE_ID,
        "plan_commitment_sha256": PLAN_COMMITMENT_SHA256,
        "authorization_id_sha256": authorization_id_sha256,
        "authorization_binding_sha256": binding_sha256,
        "authorization_expires_at_utc": canonical_expiry,
        "consumed_at_utc": _timestamp(started_at),
        "consume_before_key_load": True,
        "network_calls_at_consumption": 0,
        "model_requests_at_consumption": 0,
        "key_loaded_at_consumption": False,
        "locked_caps_accepted": True,
        "terms_and_pricing_unchanged_attested": True,
        "authorizes_retry": False,
        "authorizes_resume": False,
    }
    try:
        consumption_bytes = _exclusive_write(consumption_path, consumption)
    except FileExistsError:
        return _not_run_receipt("kimi_k3_handshake_authorization_already_consumed")
    except OSError:
        return _not_run_receipt("kimi_k3_handshake_consumption_write_failed")

    consumption_sha256 = _sha256(consumption_bytes)
    counters = _Counters()
    status = "failed"
    error_code: str | None = None
    outcome_unknown = False
    key: str | None = None
    cancelled: asyncio.CancelledError | None = None
    authorization_limited_timeout = False
    try:
        remaining_seconds = _authorization_remaining_seconds(expiry, _clock)
        authorization_limited_timeout = remaining_seconds <= RUN_TIMEOUT_SECONDS
        effective_timeout = min(float(RUN_TIMEOUT_SECONDS), remaining_seconds)
        async with asyncio.timeout(effective_timeout):
            if _key_loader is None:
                raise KimiK3HandshakeError("kimi_k3_handshake_key_loader_missing")
            try:
                key = _key_loader()
            except Exception as exc:
                raise KimiK3HandshakeError(
                    "kimi_k3_handshake_key_load_failed"
                ) from exc
            if not isinstance(key, str) or not key or key.isspace():
                raise KimiK3HandshakeError("kimi_k3_handshake_key_missing")

            first_request = _first_request()
            if len(request_body_bytes(first_request)) > 6 * 1024:
                raise KimiK3HandshakeError("kimi_k3_handshake_request_reservation_invalid")
            _authorization_remaining_seconds(expiry, _clock)
            counters.model_requests += 1
            _reserve_network_attempt(counters)
            first = await _completion_runner(
                first_request,
                api_key=key,
                confirm_online=True,
            )
            _record_response(counters, first, request_number=1)
            if first.finish_reason != "tool_calls" or len(
                first.assistant_message.tool_calls
            ) != 1:
                raise KimiK3HandshakeError(
                    "kimi_k3_handshake_required_tool_call_missing"
                )
            call = first.assistant_message.tool_calls[0]
            if call.name != TOOL_NAME:
                raise KimiK3HandshakeError("kimi_k3_handshake_tool_call_invalid")

            executor = KimiSyntheticToolExecutor()
            execution = executor.execute_batch(
                (
                    {
                        "call_id": call.call_id,
                        "name": call.name,
                        "arguments": call.arguments_json,
                    },
                )
            )
            counters.tool_executions = executor.executed_total
            if (
                execution["status"] != "completed"
                or execution["executed_tool_call_count"] != 1
                or executor.executed_total != 1
            ):
                raise KimiK3HandshakeError("kimi_k3_handshake_tool_execution_invalid")
            result = execution["results"][0]["result"]
            if result != {
                "status": "ok",
                "dataset_id": SUCCESS_DATASET_ID,
                "metric_id": METRIC_ID,
                "value": 0.375,
                "unit": "synthetic_standardized_units",
            }:
                raise KimiK3HandshakeError("kimi_k3_handshake_tool_result_invalid")

            second_request = _second_request(
                first_request, first.assistant_message, result
            )
            if len(request_body_bytes(second_request)) > 6 * 1024:
                raise KimiK3HandshakeError("kimi_k3_handshake_request_reservation_invalid")
            _authorization_remaining_seconds(expiry, _clock)
            counters.model_requests += 1
            _reserve_network_attempt(counters)
            second = await _completion_runner(
                second_request,
                api_key=key,
                confirm_online=True,
            )
            _record_response(counters, second, request_number=2)
            if second.finish_reason != "stop" or second.assistant_message.tool_calls:
                raise KimiK3HandshakeError("kimi_k3_handshake_terminal_stop_missing")

            _authorization_remaining_seconds(expiry, _clock)
            _reserve_network_attempt(counters)
            counters.invalid_request_probe_attempted = True
            probe = await _probe_runner(api_key=key, confirm_online=True)
            if isinstance(probe, KimiInvalidRequestProbeResult):
                counters.network_calls += max(0, probe.network_calls)
                counters.invalid_request_probe_http_status = probe.http_status
                if (
                    isinstance(probe.provider_error_type, str)
                    and re.fullmatch(
                        r"[a-z0-9_]{1,64}", probe.provider_error_type
                    )
                ):
                    counters.invalid_request_probe_provider_error_type = (
                        probe.provider_error_type
                    )
            if (
                not isinstance(probe, KimiInvalidRequestProbeResult)
                or probe.http_status != 400
                or probe.http_attempts != 1
                or probe.network_calls != 1
                or probe.provider_error_type != "invalid_request_error"
            ):
                raise KimiK3HandshakeError(
                    "kimi_k3_handshake_invalid_request_probe_invalid"
                )
            counters.invalid_request_probes += 1
            counters.invalid_request_probe_semantics_verified = True
            counters.probe_latency_ms = probe.latency_ms
            if (
                counters.network_attempts != NETWORK_CALL_LIMIT
                or counters.network_calls != NETWORK_CALL_LIMIT
            ):
                raise KimiK3HandshakeError("kimi_k3_handshake_network_count_invalid")
            if (
                counters.model_requests != MODEL_REQUEST_LIMIT
                or counters.tool_executions != TOOL_EXECUTION_LIMIT
                or counters.invalid_request_probes != 1
            ):
                raise KimiK3HandshakeError("kimi_k3_handshake_sequence_incomplete")
            status = "success"
    except asyncio.CancelledError as exc:
        cancelled = exc
        error_code = "kimi_k3_handshake_cancelled"
        outcome_unknown = counters.network_attempts > counters.network_calls
    except TimeoutError:
        error_code = (
            "kimi_k3_handshake_authorization_deadline_reached"
            if authorization_limited_timeout
            else "kimi_k3_handshake_total_timeout"
        )
        outcome_unknown = True
    except KimiChatTransportError as exc:
        counters.network_calls += max(0, exc.network_calls)
        if counters.invalid_request_probe_attempted:
            counters.invalid_request_probe_http_status = exc.http_status
            counters.invalid_request_probe_provider_error_type = (
                exc.provider_error_type
            )
        usage_limit_error: KimiK3HandshakeError | None = None
        if (
            exc.usage is not None
            and counters.usage_observed_model_requests < counters.model_requests
        ):
            try:
                _record_model_usage(counters, exc.usage)
            except KimiK3HandshakeError as usage_exc:
                usage_limit_error = usage_exc
        error_code = (
            usage_limit_error.code if usage_limit_error is not None else exc.code
        )
        outcome_unknown = exc.outcome_unknown
    except KimiSyntheticToolError as exc:
        error_code = exc.code
    except KimiK3HandshakeError as exc:
        error_code = exc.code
        outcome_unknown = exc.outcome_unknown
    except Exception:
        error_code = "kimi_k3_handshake_failed"
    finally:
        key = None

    if counters.network_attempts > counters.network_calls:
        outcome_unknown = True
    try:
        completed_at = _clock_now(_clock)
    except Exception:
        completed_at = datetime.now(timezone.utc)
        if status == "success":
            status = "failed"
            error_code = "kimi_k3_handshake_completion_clock_failed"
    terminal = _terminal_receipt(
        status=status,
        error_code=error_code,
        outcome_unknown=outcome_unknown,
        authorization_id_sha256=authorization_id_sha256,
        authorization_binding_sha256=binding_sha256,
        authorization_expires_at_utc=canonical_expiry,
        consumption_receipt_sha256=consumption_sha256,
        started_at=started_at,
        completed_at=completed_at,
        counters=counters,
    )
    try:
        _atomic_terminal_write(terminal_path, terminal)
    except (OSError, KimiK3HandshakeError) as exc:
        if cancelled is not None:
            raise cancelled
        code = (
            exc.code
            if isinstance(exc, KimiK3HandshakeError)
            else "kimi_k3_handshake_terminal_write_failed"
        )
        raise KimiK3HandshakeError(code, outcome_unknown=outcome_unknown) from exc
    if cancelled is not None:
        raise cancelled
    return terminal


__all__ = [
    "ARTIFACT_RELATIVE_PATH",
    "CONTRACT_RELATIVE_PATH",
    "HANDSHAKE_ID",
    "PLAN_COMMITMENT_SHA256",
    "PLAN_RELATIVE_PATH",
    "KimiK3HandshakeError",
    "kimi_k3_handshake_contract",
    "kimi_k3_handshake_plan",
    "run_kimi_k3_handshake",
    "validate_kimi_k3_handshake",
]
