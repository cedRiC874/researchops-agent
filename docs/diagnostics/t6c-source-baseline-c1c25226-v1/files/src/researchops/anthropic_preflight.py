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
from urllib.parse import quote

import certifi
import httpx

from researchops.model_providers import (
    ANTHROPIC_ALLOWED_MODEL_IDS,
    AnthropicProvider,
    ProviderConfigurationError,
)


_SCHEMA_VERSION: Final = "anthropic-models-preflight/1.0"
_PROVIDER_ID: Final = "anthropic"
_VERIFICATION_METHOD: Final = "anthropic_models_retrieve"
_API_ORIGIN: Final = "https://api.anthropic.com"
_ANTHROPIC_VERSION: Final = "2023-06-01"
_USER_AGENT: Final = (
    "researchops-agent/0.2.0 anthropic-models-preflight/1.0"
)
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

ANTHROPIC_PREFLIGHT_RECEIPT_FIELDS: Final = (
    "schema_version",
    "status",
    "provider_id",
    "requested_model_id",
    "returned_model_id",
    "verification_method",
    "api_origin",
    "anthropic_version",
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
    "messages_api_verified",
    "tool_calling_verified",
    "usage_semantics_verified",
    "model_quality_claim_allowed",
    "authorizes_model_run",
    "request_id_sha256",
    "error_code",
)

_RFC3339_DATETIME = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}"
    r"(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$"
)
_SAFE_REQUEST_ID = re.compile(r"^req_[A-Za-z0-9_-]{1,240}$")

_HTTP_ERROR_CODES: Final = {
    400: "anthropic_preflight_invalid_request_or_spend_limit",
    401: "anthropic_preflight_auth_failed",
    402: "anthropic_preflight_billing_blocked",
    403: "anthropic_preflight_permission_denied",
    404: "anthropic_preflight_model_unavailable",
    408: "anthropic_preflight_provider_timeout",
    409: "anthropic_preflight_conflict",
    413: "anthropic_preflight_protocol_failed",
    429: "anthropic_preflight_rate_or_spend_limited",
    504: "anthropic_preflight_provider_timeout",
}

_STABLE_ERROR_CODES: Final = (
    "anthropic_preflight_confirmation_required",
    "anthropic_preflight_key_missing",
    "anthropic_preflight_key_invalid",
    "anthropic_preflight_model_not_allowed",
    "anthropic_preflight_configuration_invalid",
    "anthropic_preflight_redirect_denied",
    "anthropic_preflight_invalid_request_or_spend_limit",
    "anthropic_preflight_auth_failed",
    "anthropic_preflight_billing_blocked",
    "anthropic_preflight_permission_denied",
    "anthropic_preflight_model_unavailable",
    "anthropic_preflight_provider_timeout",
    "anthropic_preflight_conflict",
    "anthropic_preflight_protocol_failed",
    "anthropic_preflight_rate_or_spend_limited",
    "anthropic_preflight_provider_unavailable",
    "anthropic_preflight_timeout",
    "anthropic_preflight_network_failed",
    "anthropic_preflight_identity_mismatch",
    "anthropic_preflight_response_invalid",
    "anthropic_preflight_client_close_failed",
    "anthropic_preflight_failed",
)


def anthropic_models_preflight_contract() -> dict[str, Any]:
    """Return the machine-readable offline contract without networking."""

    return {
        "schema_version": "1.0",
        "contract_id": "eval-v2-anthropic-models-preflight-v1",
        "implementation_status": "implemented_offline_tested_not_run",
        "provider": {
            "provider_id": _PROVIDER_ID,
            "allowed_model_ids": list(ANTHROPIC_ALLOWED_MODEL_IDS),
            "api_origin": _API_ORIGIN,
            "api_key_environment_variable": "ANTHROPIC_API_KEY",
        },
        "request_contract": {
            "method": "GET",
            "path_template": "/v1/models/{percent_encoded_exact_model_id}",
            "anthropic_version": _ANTHROPIC_VERSION,
            "required_application_headers": {
                "accept": "application/json",
                "accept-encoding": "identity",
                "anthropic-version": _ANTHROPIC_VERSION,
                "user-agent": _USER_AGENT,
                "x-api-key": "memory_only_secret",
            },
            "forbidden_application_headers": [
                "anthropic-beta",
                "authorization",
                "content-type",
            ],
            "body_absent": True,
            "raw_model_must_equal_normalized_allowlisted_model": True,
            "messages_or_completions_called": False,
            "model_token_calls": 0,
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
            "raw_error_body_persisted_or_logged": False,
            "api_key_persisted_logged_or_unkeyed_hashed": False,
            "api_key_loaded_after_local_gates": True,
            "anthropic_online_transport_default_denied": True,
            "offline_test_capability_single_use": True,
            "supported_production_authorization_factory_available": False,
            "private_python_test_seams_are_security_boundary": False,
        },
        "receipt_contract": {
            "schema_version": _SCHEMA_VERSION,
            "fields": list(ANTHROPIC_PREFLIGHT_RECEIPT_FIELDS),
            "statuses": ["not_run", "verified", "failed"],
            "invalid_model_requested_model_id_is_null": True,
            "token_usage": None,
            "cost": None,
            "authorizes_model_run": False,
            "raw_request_id_persisted": False,
            "request_id_hash_scope": "local_correlation_only",
            "stable_error_codes": list(_STABLE_ERROR_CODES),
        },
        "entry_points": {
            "models_preflight_cli_enabled": True,
            "generic_phase6_anthropic_enabled": False,
            "generic_self_pilot_anthropic_enabled": False,
            "self_pilot_web_anthropic_enabled": False,
            "eval_v2_public_runner_anthropic_enabled": False,
            "controlled_anthropic_pilot_enabled": False,
        },
        "evaluation_boundary": {
            "live_models_preflight_performed": False,
            "messages_or_tool_calls_performed": False,
            "campaign_registered": False,
            "public_candidate_authorized": False,
            "private_access_authorized": False,
            "online_calls_performed": False,
            "model_quality_claim_allowed": False,
            "prior_results_inherited": False,
            "offline_test_network_calls": 0,
        },
        "official_references": {
            "get_model": "https://platform.claude.com/docs/en/api/models/retrieve",
            "errors": "https://platform.claude.com/docs/en/api/errors",
            "python_sdk": (
                "https://platform.claude.com/docs/en/cli-sdks-libraries/"
                "sdks/python"
            ),
        },
    }


class _ResponseTooLarge(Exception):
    pass


class _ResponseInvalid(Exception):
    pass


class _IdentityMismatch(Exception):
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
    """Build the only persistable representation of a preflight result.

    ``requested_model_id`` is null only when the caller supplied an invalid model.
    Echoing an arbitrary rejected string would violate the receipt allowlist and
    could turn the diagnostic result into a data-exfiltration surface.
    """

    result: dict[str, Any] = {
        "schema_version": _SCHEMA_VERSION,
        "status": status,
        "provider_id": _PROVIDER_ID,
        "requested_model_id": requested_model_id,
        "returned_model_id": returned_model_id,
        "verification_method": _VERIFICATION_METHOD,
        "api_origin": _API_ORIGIN,
        "anthropic_version": _ANTHROPIC_VERSION,
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
        "messages_api_verified": False,
        "tool_calling_verified": False,
        "usage_semantics_verified": False,
        "model_quality_claim_allowed": False,
        "authorizes_model_run": False,
        "request_id_sha256": request_id_sha256,
        "error_code": error_code,
    }
    if tuple(result) != ANTHROPIC_PREFLIGHT_RECEIPT_FIELDS:
        raise AssertionError("Anthropic preflight receipt field drift")
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
    if not isinstance(model_id, str):
        return None
    try:
        normalized = AnthropicProvider().validate_model(model_id)
    except ProviderConfigurationError:
        return None
    if model_id != normalized:
        return None
    return normalized


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
        "anthropic-version": _ANTHROPIC_VERSION,
        "x-api-key": api_key,
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


def _is_rfc3339_datetime(value: object) -> bool:
    if not isinstance(value, str) or not _RFC3339_DATETIME.fullmatch(value):
        return False
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return parsed.tzinfo is not None and parsed.utcoffset() is not None


def _is_optional_nonnegative_integer(value: object) -> bool:
    return value is None or (type(value) is int and value >= 0)


def _validate_success_payload(
    payload: dict[str, Any], requested_model_id: str
) -> str:
    if "type" not in payload or "id" not in payload:
        raise _ResponseInvalid
    if not isinstance(payload["type"], str) or not isinstance(payload["id"], str):
        raise _ResponseInvalid
    if payload["type"] != "model" or payload["id"] != requested_model_id:
        raise _IdentityMismatch

    required_fields = {
        "created_at",
        "display_name",
        "capabilities",
        "max_input_tokens",
        "max_tokens",
    }
    if not required_fields.issubset(payload):
        raise _ResponseInvalid
    if not _is_rfc3339_datetime(payload["created_at"]):
        raise _ResponseInvalid
    display_name = payload["display_name"]
    if not isinstance(display_name, str) or not display_name.strip():
        raise _ResponseInvalid
    capabilities = payload["capabilities"]
    if capabilities is not None and not isinstance(capabilities, dict):
        raise _ResponseInvalid
    if not _is_optional_nonnegative_integer(payload["max_input_tokens"]):
        raise _ResponseInvalid
    if not _is_optional_nonnegative_integer(payload["max_tokens"]):
        raise _ResponseInvalid
    return payload["id"]


def _safe_returned_model_id(value: object, api_key: str) -> str | None:
    if not isinstance(value, str) or value == api_key:
        return None
    return _validated_model_id(value)


def _request_id_hash(response: httpx.Response, api_key: str) -> str | None:
    value = response.headers.get("request-id")
    if (
        not value
        or not _SAFE_REQUEST_ID.fullmatch(value)
        or api_key in value
        or value in api_key
    ):
        return None
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _http_error_code(status_code: int) -> str:
    if 300 <= status_code < 400:
        return "anthropic_preflight_redirect_denied"
    if status_code in _HTTP_ERROR_CODES:
        return _HTTP_ERROR_CODES[status_code]
    if 500 <= status_code < 600:
        return "anthropic_preflight_provider_unavailable"
    return "anthropic_preflight_failed"


async def run_anthropic_models_preflight(
    *,
    provider_id: str,
    model_id: object,
    api_key: str | None,
    confirm_online: bool,
    _key_loader: Callable[[], str | None] | None = None,
    _transport_factory: Callable[[], httpx.AsyncBaseTransport] | None = None,
) -> dict[str, Any]:
    """Perform one fixed Anthropic Models API metadata preflight.

    The underscore-prefixed transport factory is an offline-test seam. Production
    callers must leave it unset. The function never reads environment variables,
    retries, follows redirects, calls a model-generation endpoint, or raises a
    provider error containing response/credential material. Caller cancellation is
    deliberately propagated after owned-client cleanup.
    """

    if api_key is not None and _key_loader is not None:
        return _not_run(
            requested_model_id=_validated_model_id(model_id),
            error_code="anthropic_preflight_configuration_invalid",
        )
    if confirm_online is not True:
        return _not_run(
            requested_model_id=_validated_model_id(model_id),
            error_code="anthropic_preflight_confirmation_required",
        )
    safe_model_id = _validated_model_id(model_id)
    if provider_id != _PROVIDER_ID:
        return _not_run(
            requested_model_id=safe_model_id,
            error_code="anthropic_preflight_configuration_invalid",
        )
    if safe_model_id is None:
        return _not_run(
            requested_model_id=None,
            error_code="anthropic_preflight_model_not_allowed",
        )
    if not _configuration_is_valid():
        return _not_run(
            requested_model_id=safe_model_id,
            error_code="anthropic_preflight_configuration_invalid",
        )
    if _key_loader is not None:
        try:
            api_key = _key_loader()
        except Exception:
            return _not_run(
                requested_model_id=safe_model_id,
                error_code="anthropic_preflight_configuration_invalid",
            )
    if not isinstance(api_key, str) or not api_key or api_key.isspace():
        return _not_run(
            requested_model_id=safe_model_id,
            error_code="anthropic_preflight_key_missing",
        )
    if not _is_safe_api_key(api_key):
        return _not_run(
            requested_model_id=safe_model_id,
            error_code="anthropic_preflight_key_invalid",
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
            error_code = "anthropic_preflight_failed"

        if client is not None:
            attempts = 1
            path = f"/v1/models/{quote(safe_model_id, safe='')}"
            try:
                async with asyncio.timeout(_TOTAL_DEADLINE_SECONDS):
                    async with client.stream(
                        "GET",
                        path,
                        headers=_request_headers(api_key),
                    ) as response:
                        http_status = response.status_code
                        request_id_sha256 = _request_id_hash(response, api_key)
                        if http_status != 200:
                            error_code = _http_error_code(http_status)
                            if http_status == 401:
                                authenticated = False
                        else:
                            authenticated = True
                            content_encoding = response.headers.get(
                                "content-encoding"
                            )
                            if (
                                content_encoding is not None
                                and content_encoding.strip().lower() != "identity"
                            ):
                                raise _ResponseInvalid
                            body = await _read_bounded_decoded_body(response)
                            payload = _decode_json_object(body)
                            raw_returned_id = payload.get("id")
                            returned_model_id = _safe_returned_model_id(
                                raw_returned_id, api_key
                            )
                            exact_id = _validate_success_payload(
                                payload, safe_model_id
                            )
                            returned_model_id = exact_id
                            visible = True
                            verified = True
            except asyncio.CancelledError:
                raise
            except (TimeoutError, httpx.TimeoutException):
                error_code = "anthropic_preflight_timeout"
                verified = False
            except httpx.DecodingError:
                error_code = "anthropic_preflight_response_invalid"
                verified = False
            except httpx.RequestError:
                error_code = "anthropic_preflight_network_failed"
                verified = False
            except _IdentityMismatch:
                error_code = "anthropic_preflight_identity_mismatch"
                verified = False
            except (_ResponseTooLarge, _ResponseInvalid):
                error_code = "anthropic_preflight_response_invalid"
                verified = False
            except Exception:
                error_code = "anthropic_preflight_failed"
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
                error_code = "anthropic_preflight_client_close_failed"
                verified = False
        finally:
            # This shortens the local lifetime only; it is not secure zeroization.
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
        returned_model_id=returned_model_id,
        latency_ms=latency_ms if attempts else None,
        http_status=http_status,
        http_attempts=attempts,
        network_calls=attempts,
        models_api_authenticated=authenticated,
        exact_model_visible=visible,
        request_id_sha256=request_id_sha256,
        error_code=error_code or "anthropic_preflight_failed",
    )
