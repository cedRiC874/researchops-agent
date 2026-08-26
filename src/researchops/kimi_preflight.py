from __future__ import annotations

import asyncio
import hashlib
import importlib.metadata
import json
import logging
import math
import re
import ssl
import time
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any, Final

import certifi
import httpx


_SCHEMA_VERSION: Final = "kimi-models-preflight/1.0"
_PROVIDER_ID: Final = "moonshot_kimi"
_MODEL_ID: Final = "kimi-k3"
_VERIFICATION_METHOD: Final = "kimi_models_list"
_API_ORIGIN: Final = "https://api.moonshot.cn"
_MODELS_PATH: Final = "/v1/models"
_USER_AGENT: Final = "researchops-agent/0.2.0 kimi-models-preflight/1.0"
_EXPECTED_HTTPX_VERSION: Final = "0.28.1"
_EXPECTED_CERTIFI_VERSION: Final = "2026.7.22"
_EXPECTED_HTTPCORE_VERSION: Final = "1.0.9"
_EXPECTED_H11_VERSION: Final = "0.16.0"
_MAX_DECODED_BODY_BYTES: Final = 64 * 1024
_CONNECT_TIMEOUT_SECONDS: Final = 5.0
_READ_TIMEOUT_SECONDS: Final = 10.0
_WRITE_TIMEOUT_SECONDS: Final = 5.0
_POOL_TIMEOUT_SECONDS: Final = 5.0
_TOTAL_DEADLINE_SECONDS: Final = 15.0
_CLOSE_TIMEOUT_SECONDS: Final = 5.0
_HTTP_DEBUG_LOGGER_NAMES: Final = (
    "httpx",
    "httpcore",
    "httpcore.connection",
    "httpcore.http11",
    "httpcore.http2",
    "httpcore.proxy",
    "httpcore.socks",
)

KIMI_PREFLIGHT_RECEIPT_FIELDS: Final = (
    "schema_version",
    "status",
    "provider_id",
    "requested_model_id",
    "returned_model_id",
    "verification_method",
    "api_origin",
    "checked_at_utc",
    "latency_ms",
    "http_status",
    "http_attempts",
    "network_calls",
    "model_token_calls",
    "token_usage",
    "cost",
    "models_api_authenticated",
    "exact_model_visible",
    "chat_completions_verified",
    "responses_api_verified",
    "tool_calling_verified",
    "usage_semantics_verified",
    "model_quality_claim_allowed",
    "authorizes_model_run",
    "authorizes_provider_registration",
    "request_id_sha256",
    "error_code",
)

_SAFE_REQUEST_ID = re.compile(r"^[A-Za-z0-9_-]{1,240}$")

_HTTP_ERROR_CODES: Final = {
    400: "kimi_preflight_invalid_request",
    401: "kimi_preflight_auth_failed",
    402: "kimi_preflight_billing_blocked",
    403: "kimi_preflight_permission_denied",
    404: "kimi_preflight_resource_not_found",
    408: "kimi_preflight_provider_timeout",
    409: "kimi_preflight_conflict",
    413: "kimi_preflight_protocol_failed",
    499: "kimi_preflight_client_closed_request",
    504: "kimi_preflight_provider_timeout",
}
_RATE_LIMIT_ERROR_CODES: Final = {
    "engine_overloaded_error": "kimi_preflight_engine_overloaded",
    "exceeded_current_quota_error": "kimi_preflight_quota_exceeded",
    "rate_limit_reached_error": "kimi_preflight_rate_limited",
}
_STABLE_ERROR_CODES: Final = (
    "kimi_preflight_confirmation_required",
    "kimi_preflight_key_missing",
    "kimi_preflight_key_invalid",
    "kimi_preflight_model_not_allowed",
    "kimi_preflight_configuration_invalid",
    "kimi_preflight_redirect_denied",
    "kimi_preflight_invalid_request",
    "kimi_preflight_auth_failed",
    "kimi_preflight_billing_blocked",
    "kimi_preflight_permission_denied",
    "kimi_preflight_resource_not_found",
    "kimi_preflight_provider_timeout",
    "kimi_preflight_conflict",
    "kimi_preflight_protocol_failed",
    "kimi_preflight_client_closed_request",
    "kimi_preflight_engine_overloaded",
    "kimi_preflight_quota_exceeded",
    "kimi_preflight_rate_limited",
    "kimi_preflight_rate_or_quota_limited",
    "kimi_preflight_provider_unavailable",
    "kimi_preflight_timeout",
    "kimi_preflight_network_failed",
    "kimi_preflight_identity_mismatch",
    "kimi_preflight_model_not_visible",
    "kimi_preflight_response_invalid",
    "kimi_preflight_client_close_failed",
    "kimi_preflight_failed",
)


def kimi_models_preflight_contract() -> dict[str, Any]:
    """Return the machine-readable Kimi preflight contract without networking."""

    return {
        "schema_version": "1.0",
        "contract_id": "eval-v2-kimi-models-preflight-v1",
        "implementation_status": "implemented_offline_tested_not_run",
        "provider": {
            "provider_id": _PROVIDER_ID,
            "provider_owner": "Moonshot AI",
            "allowed_model_ids": [_MODEL_ID],
            "api_origin": _API_ORIGIN,
            "api_key_environment_variable": "MOONSHOT_API_KEY",
            "china_platform_only": True,
            "international_platform_key_compatible": False,
        },
        "request_contract": {
            "method": "GET",
            "path": _MODELS_PATH,
            "required_application_headers": {
                "accept": "application/json",
                "accept-encoding": "identity",
                "authorization": "Bearer memory_only_secret",
                "user-agent": _USER_AGENT,
            },
            "forbidden_application_headers": ["content-type", "x-api-key"],
            "body_absent": True,
            "exact_model_id": _MODEL_ID,
            "chat_completions_called": False,
            "responses_called": False,
            "model_token_calls": 0,
        },
        "response_contract": {
            "success_media_type": "application/json",
            "charset_parameter_allowed": True,
            "missing_or_non_json_success_media_type_allowed": False,
            "documented_429_classification_requires_json_media_type": True,
        },
        "runtime_controls": {
            "httpx_version": _EXPECTED_HTTPX_VERSION,
            "certifi_version": _EXPECTED_CERTIFI_VERSION,
            "httpcore_version": _EXPECTED_HTTPCORE_VERSION,
            "h11_version": _EXPECTED_H11_VERSION,
            "owned_client_per_preflight": True,
            "trust_env": False,
            "tls_verification_required": True,
            "follow_redirects": False,
            "http_attempts_max": 1,
            "provider_managed_retries": 0,
            "fallbacks_allowed": False,
            "response_streamed": True,
            "max_decoded_body_bytes": _MAX_DECODED_BODY_BYTES,
            "timeouts_seconds": {
                "connect": _CONNECT_TIMEOUT_SECONDS,
                "read": _READ_TIMEOUT_SECONDS,
                "write": _WRITE_TIMEOUT_SECONDS,
                "pool": _POOL_TIMEOUT_SECONDS,
                "request_total": _TOTAL_DEADLINE_SECONDS,
                "cleanup_close": _CLOSE_TIMEOUT_SECONDS,
            },
            "external_tracing_or_callbacks_allowed": False,
            "http_debug_logging_allowed": False,
            "http_debug_logger_names": list(_HTTP_DEBUG_LOGGER_NAMES),
            "non_identity_content_encoding_allowed": False,
            "raw_success_or_error_body_persisted_or_logged": False,
            "api_key_persisted_logged_or_unkeyed_hashed": False,
            "api_key_loaded_after_local_gates": True,
            "online_transport_default_denied": True,
            "offline_test_transport_factory_single_use": False,
            "external_authorization_consumption_ledger_implemented": False,
            "private_python_test_seams_are_security_boundary": False,
        },
        "receipt_contract": {
            "schema_version": _SCHEMA_VERSION,
            "fields": list(KIMI_PREFLIGHT_RECEIPT_FIELDS),
            "statuses": ["not_run", "verified", "failed"],
            "invalid_model_requested_model_id_is_null": True,
            "token_usage": None,
            "cost": None,
            "authorizes_model_run": False,
            "authorizes_provider_registration": False,
            "raw_request_id_persisted": False,
            "request_id_hash_scope": "local_correlation_only",
            "stable_error_codes": list(_STABLE_ERROR_CODES),
        },
        "capability_boundary": {
            "list_models_is_account_specific": True,
            "models_visibility_verifies_chat_completions": False,
            "models_visibility_verifies_responses": False,
            "models_visibility_verifies_tool_calling": False,
            "models_visibility_verifies_usage_semantics": False,
            "responses_api_documented": False,
        },
        "entry_points": {
            "models_preflight_cli_enabled": True,
            "generic_phase6_kimi_enabled": False,
            "generic_self_pilot_kimi_enabled": False,
            "eval_v2_public_runner_kimi_enabled": False,
            "controlled_kimi_pilot_enabled": False,
        },
        "evaluation_boundary": {
            "live_models_preflight_performed": False,
            "chat_completions_or_tool_calls_performed": False,
            "campaign_registered": False,
            "public_candidate_authorized": False,
            "private_access_authorized": False,
            "online_calls_performed": False,
            "model_quality_claim_allowed": False,
            "prior_results_inherited": False,
            "offline_test_network_calls": 0,
        },
        "official_references": {
            "api_overview": "https://platform.kimi.com/docs/api/overview",
            "list_models": "https://platform.kimi.com/docs/api/list-models",
            "errors": "https://platform.kimi.com/docs/api/errors",
            "openapi": "https://platform.kimi.com/docs/openapi.json",
        },
    }


class _ResponseTooLarge(Exception):
    pass


class _ResponseInvalid(Exception):
    pass


class _IdentityMismatch(Exception):
    pass


class _ModelNotVisible(Exception):
    pass


def _checked_at_utc() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def _receipt(
    *,
    status: str,
    requested_model_id: str | None,
    returned_model_id: str | None,
    latency_ms: int | None,
    http_status: int | None,
    http_attempts: int,
    network_calls: int,
    models_api_authenticated: bool | None,
    exact_model_visible: bool | None,
    request_id_sha256: str | None,
    error_code: str | None,
) -> dict[str, Any]:
    """Build the only persistable representation of a preflight result."""

    result: dict[str, Any] = {
        "schema_version": _SCHEMA_VERSION,
        "status": status,
        "provider_id": _PROVIDER_ID,
        "requested_model_id": requested_model_id,
        "returned_model_id": returned_model_id,
        "verification_method": _VERIFICATION_METHOD,
        "api_origin": _API_ORIGIN,
        "checked_at_utc": _checked_at_utc(),
        "latency_ms": latency_ms,
        "http_status": http_status,
        "http_attempts": http_attempts,
        "network_calls": network_calls,
        "model_token_calls": 0,
        "token_usage": None,
        "cost": None,
        "models_api_authenticated": models_api_authenticated,
        "exact_model_visible": exact_model_visible,
        "chat_completions_verified": False,
        "responses_api_verified": False,
        "tool_calling_verified": False,
        "usage_semantics_verified": False,
        "model_quality_claim_allowed": False,
        "authorizes_model_run": False,
        "authorizes_provider_registration": False,
        "request_id_sha256": request_id_sha256,
        "error_code": error_code,
    }
    if tuple(result) != KIMI_PREFLIGHT_RECEIPT_FIELDS:
        raise AssertionError("Kimi preflight receipt field drift")
    return result


def _not_run(
    *, requested_model_id: str | None, error_code: str
) -> dict[str, Any]:
    return _receipt(
        status="not_run",
        requested_model_id=requested_model_id,
        returned_model_id=None,
        latency_ms=None,
        http_status=None,
        http_attempts=0,
        network_calls=0,
        models_api_authenticated=None,
        exact_model_visible=None,
        request_id_sha256=None,
        error_code=error_code,
    )


def _configuration_is_valid() -> bool:
    timeout_values = (
        _CONNECT_TIMEOUT_SECONDS,
        _READ_TIMEOUT_SECONDS,
        _WRITE_TIMEOUT_SECONDS,
        _POOL_TIMEOUT_SECONDS,
        _TOTAL_DEADLINE_SECONDS,
        _CLOSE_TIMEOUT_SECONDS,
    )
    if any(
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
        or float(value) <= 0
        for value in timeout_values
    ):
        return False
    if _TOTAL_DEADLINE_SECONDS < max(
        _CONNECT_TIMEOUT_SECONDS,
        _READ_TIMEOUT_SECONDS,
        _WRITE_TIMEOUT_SECONDS,
        _POOL_TIMEOUT_SECONDS,
    ):
        return False
    if (
        _API_ORIGIN != "https://api.moonshot.cn"
        or _MODELS_PATH != "/v1/models"
        or _MAX_DECODED_BODY_BYTES != 64 * 1024
    ):
        return False
    if any(
        logging.getLogger(name).isEnabledFor(logging.DEBUG)
        for name in _HTTP_DEBUG_LOGGER_NAMES
    ):
        return False
    try:
        return (
            importlib.metadata.version("httpx") == _EXPECTED_HTTPX_VERSION
            and importlib.metadata.version("certifi") == _EXPECTED_CERTIFI_VERSION
            and importlib.metadata.version("httpcore")
            == _EXPECTED_HTTPCORE_VERSION
            and importlib.metadata.version("h11") == _EXPECTED_H11_VERSION
        )
    except Exception:
        return False


def _validated_model_id(model_id: object) -> str | None:
    if type(model_id) is not str or model_id != _MODEL_ID:
        return None
    return _MODEL_ID


def _is_safe_api_key(value: str) -> bool:
    return (
        1 <= len(value) <= 512
        and value == value.strip()
        and all(0x21 <= ord(character) <= 0x7E for character in value)
    )


def _default_transport_factory() -> httpx.AsyncBaseTransport:
    tls_context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    tls_context.verify_mode = ssl.CERT_REQUIRED
    tls_context.check_hostname = True
    tls_context.load_verify_locations(cafile=certifi.where())
    tls_context.keylog_filename = None
    return httpx.AsyncHTTPTransport(
        verify=tls_context,
        trust_env=False,
        retries=0,
    )


def _build_client(transport: httpx.AsyncBaseTransport) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        base_url=_API_ORIGIN,
        transport=transport,
        timeout=httpx.Timeout(
            connect=_CONNECT_TIMEOUT_SECONDS,
            read=_READ_TIMEOUT_SECONDS,
            write=_WRITE_TIMEOUT_SECONDS,
            pool=_POOL_TIMEOUT_SECONDS,
        ),
        follow_redirects=False,
        trust_env=False,
        verify=True,
        auth=None,
        event_hooks={},
    )


def _request_headers(api_key: str) -> dict[str, str]:
    return {
        "authorization": f"Bearer {api_key}",
        "accept": "application/json",
        "accept-encoding": "identity",
        "user-agent": _USER_AGENT,
    }


async def _read_bounded_decoded_body(response: httpx.Response) -> bytes:
    body = bytearray()
    size = 0
    async for chunk in response.aiter_bytes():
        size += len(chunk)
        if size > _MAX_DECODED_BODY_BYTES:
            raise _ResponseTooLarge
        body.extend(chunk)
    return bytes(body)


def _reject_json_constant(value: str) -> None:
    del value
    raise _ResponseInvalid


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _ResponseInvalid
        result[key] = value
    return result


def _decode_json_object(body: bytes) -> dict[str, Any]:
    try:
        payload = json.loads(
            body,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_json_constant,
        )
    except (
        UnicodeDecodeError,
        ValueError,
        RecursionError,
        _ResponseInvalid,
    ) as exc:
        raise _ResponseInvalid from exc
    if not isinstance(payload, dict):
        raise _ResponseInvalid
    return payload


def _valid_nonnegative_integer(value: object) -> bool:
    return type(value) is int and value >= 0


def _validate_target_model(model: dict[str, Any]) -> str:
    if model.get("id") != _MODEL_ID or model.get("object") != "model":
        raise _IdentityMismatch
    required_fields = {
        "created",
        "owned_by",
        "context_length",
        "supports_image_in",
        "supports_video_in",
        "supports_reasoning",
    }
    if not required_fields.issubset(model):
        raise _ResponseInvalid
    if not _valid_nonnegative_integer(model["created"]):
        raise _ResponseInvalid
    if model["owned_by"] != "moonshot":
        raise _IdentityMismatch
    if (
        not _valid_nonnegative_integer(model["context_length"])
        or model["context_length"] == 0
    ):
        raise _ResponseInvalid
    for field in (
        "supports_image_in",
        "supports_video_in",
        "supports_reasoning",
    ):
        if type(model[field]) is not bool:
            raise _ResponseInvalid
    return _MODEL_ID


def _validate_success_payload(payload: dict[str, Any]) -> str | None:
    if payload.get("object") != "list" or not isinstance(
        payload.get("data"), list
    ):
        raise _ResponseInvalid
    seen_ids: set[str] = set()
    target: dict[str, Any] | None = None
    for item in payload["data"]:
        if not isinstance(item, dict):
            raise _ResponseInvalid
        model_id = item.get("id")
        if not isinstance(model_id, str) or item.get("object") != "model":
            raise _ResponseInvalid
        if model_id in seen_ids:
            raise _ResponseInvalid
        seen_ids.add(model_id)
        if model_id == _MODEL_ID:
            target = item
    if target is None:
        return None
    return _validate_target_model(target)


def _request_id_hash(response: httpx.Response, api_key: str) -> str | None:
    value = response.headers.get("msh-request-id")
    if (
        not value
        or not _SAFE_REQUEST_ID.fullmatch(value)
        or api_key in value
        or value in api_key
    ):
        return None
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _content_encoding_is_identity(response: httpx.Response) -> bool:
    value = response.headers.get("content-encoding")
    return value is None or value.strip().lower() == "identity"


def _content_type_is_json(response: httpx.Response) -> bool:
    value = response.headers.get("content-type")
    if value is None:
        return False
    media_type = value.split(";", 1)[0].strip().lower()
    return media_type == "application/json"


async def _rate_limit_error_code(response: httpx.Response) -> str:
    """Classify only the three documented 429 types without retaining content."""

    if not _content_encoding_is_identity(response) or not _content_type_is_json(
        response
    ):
        return "kimi_preflight_rate_or_quota_limited"
    try:
        body = await _read_bounded_decoded_body(response)
        payload = _decode_json_object(body)
        error = payload.get("error")
        if not isinstance(error, dict):
            raise _ResponseInvalid
        error_type = error.get("type")
        if not isinstance(error_type, str):
            raise _ResponseInvalid
    except (
        httpx.DecodingError,
        _ResponseTooLarge,
        _ResponseInvalid,
    ):
        return "kimi_preflight_rate_or_quota_limited"
    return _RATE_LIMIT_ERROR_CODES.get(
        error_type, "kimi_preflight_rate_or_quota_limited"
    )


def _http_error_code(status_code: int) -> str:
    if 300 <= status_code < 400:
        return "kimi_preflight_redirect_denied"
    if status_code in _HTTP_ERROR_CODES:
        return _HTTP_ERROR_CODES[status_code]
    if 500 <= status_code < 600:
        return "kimi_preflight_provider_unavailable"
    return "kimi_preflight_failed"


async def run_kimi_models_preflight(
    *,
    provider_id: str = _PROVIDER_ID,
    model_id: object = _MODEL_ID,
    api_key: str | None = None,
    confirm_online: bool = False,
    _key_loader: Callable[[], str | None] | None = None,
    _transport_factory: Callable[[], httpx.AsyncBaseTransport] | None = None,
) -> dict[str, Any]:
    """Perform one fixed Kimi China Models API metadata preflight.

    The function does not read environment variables. It performs no request unless
    ``confirm_online`` is exactly true and a validated in-memory key is supplied or
    loaded after all local gates. It never retries, follows redirects, falls back,
    calls a generation endpoint, or persists response and credential material.
    The underscore-prefixed transport factory is an offline-test seam, not a
    production authorization boundary. Caller cancellation propagates after
    owned-client cleanup.
    """

    safe_model_id = _validated_model_id(model_id)
    if api_key is not None and _key_loader is not None:
        return _not_run(
            requested_model_id=safe_model_id,
            error_code="kimi_preflight_configuration_invalid",
        )
    if confirm_online is not True:
        return _not_run(
            requested_model_id=safe_model_id,
            error_code="kimi_preflight_confirmation_required",
        )
    if provider_id != _PROVIDER_ID:
        return _not_run(
            requested_model_id=safe_model_id,
            error_code="kimi_preflight_configuration_invalid",
        )
    if safe_model_id is None:
        return _not_run(
            requested_model_id=None,
            error_code="kimi_preflight_model_not_allowed",
        )
    if not _configuration_is_valid():
        return _not_run(
            requested_model_id=safe_model_id,
            error_code="kimi_preflight_configuration_invalid",
        )
    if _key_loader is not None:
        try:
            api_key = _key_loader()
        except Exception:
            return _not_run(
                requested_model_id=safe_model_id,
                error_code="kimi_preflight_configuration_invalid",
            )
    if not isinstance(api_key, str) or not api_key or api_key.isspace():
        return _not_run(
            requested_model_id=safe_model_id,
            error_code="kimi_preflight_key_missing",
        )
    if not _is_safe_api_key(api_key):
        return _not_run(
            requested_model_id=safe_model_id,
            error_code="kimi_preflight_key_invalid",
        )

    factory = _transport_factory or _default_transport_factory
    transport: httpx.AsyncBaseTransport | None = None
    client: httpx.AsyncClient | None = None
    started_ns = time.perf_counter_ns()
    attempts = 0
    http_status: int | None = None
    returned_model_id: str | None = None
    authenticated: bool | None = None
    visible: bool | None = None
    request_id_sha256: str | None = None
    error_code: str | None = None
    verified = False

    try:
        try:
            transport = factory()
            client = _build_client(transport)
        except asyncio.CancelledError:
            raise
        except Exception:
            error_code = "kimi_preflight_failed"

        if client is not None:
            attempts = 1
            try:
                async with asyncio.timeout(_TOTAL_DEADLINE_SECONDS):
                    async with client.stream(
                        "GET",
                        _MODELS_PATH,
                        headers=_request_headers(api_key),
                    ) as response:
                        http_status = response.status_code
                        request_id_sha256 = _request_id_hash(response, api_key)
                        if http_status == 429:
                            error_code = await _rate_limit_error_code(response)
                        elif http_status != 200:
                            error_code = _http_error_code(http_status)
                            if http_status == 401:
                                authenticated = False
                        else:
                            if not _content_encoding_is_identity(
                                response
                            ) or not _content_type_is_json(response):
                                raise _ResponseInvalid
                            body = await _read_bounded_decoded_body(response)
                            payload = _decode_json_object(body)
                            returned_model_id = _validate_success_payload(payload)
                            authenticated = True
                            if returned_model_id is None:
                                raise _ModelNotVisible
                            visible = True
                            verified = True
            except asyncio.CancelledError:
                raise
            except (TimeoutError, httpx.TimeoutException):
                error_code = "kimi_preflight_timeout"
                verified = False
            except httpx.DecodingError:
                error_code = "kimi_preflight_response_invalid"
                verified = False
            except httpx.RequestError:
                error_code = "kimi_preflight_network_failed"
                verified = False
            except _IdentityMismatch:
                error_code = "kimi_preflight_identity_mismatch"
                verified = False
            except _ModelNotVisible:
                error_code = "kimi_preflight_model_not_visible"
                visible = False
                verified = False
            except (_ResponseTooLarge, _ResponseInvalid):
                error_code = "kimi_preflight_response_invalid"
                verified = False
            except Exception:
                error_code = "kimi_preflight_failed"
                verified = False
    finally:
        try:
            if client is not None:
                async with asyncio.timeout(_CLOSE_TIMEOUT_SECONDS):
                    await client.aclose()
            elif transport is not None:
                async with asyncio.timeout(_CLOSE_TIMEOUT_SECONDS):
                    await transport.aclose()
        except asyncio.CancelledError:
            raise
        except Exception:
            if error_code is None:
                error_code = "kimi_preflight_client_close_failed"
                verified = False
        finally:
            # This shortens local lifetime only; it is not secure zeroization.
            api_key = None

    latency_ms = max(0, (time.perf_counter_ns() - started_ns) // 1_000_000)
    if verified and error_code is None:
        return _receipt(
            status="verified",
            requested_model_id=safe_model_id,
            returned_model_id=returned_model_id,
            latency_ms=latency_ms,
            http_status=http_status,
            http_attempts=attempts,
            network_calls=attempts,
            models_api_authenticated=True,
            exact_model_visible=True,
            request_id_sha256=request_id_sha256,
            error_code=None,
        )

    return _receipt(
        status="failed" if attempts else "not_run",
        requested_model_id=safe_model_id,
        returned_model_id=None,
        latency_ms=latency_ms if attempts else None,
        http_status=http_status,
        http_attempts=attempts,
        network_calls=attempts,
        models_api_authenticated=authenticated,
        exact_model_visible=visible,
        request_id_sha256=request_id_sha256,
        error_code=error_code or "kimi_preflight_failed",
    )
