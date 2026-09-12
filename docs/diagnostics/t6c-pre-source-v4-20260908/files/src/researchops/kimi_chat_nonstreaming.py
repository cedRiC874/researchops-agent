from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Final, Mapping

import httpx

from . import kimi_chat_transport as _v1
from .kimi_chat_transport import (
    KimiAssistantMessage,
    KimiChatRequest,
    KimiChatTransportError,
    KimiSpecifiedToolChoice,
    KimiToolCall,
)
from .kimi_chat_transport_v3 import KimiChatUsageV3


KIMI_NONSTREAMING_TRANSPORT_ID: Final = (
    "moonshot_direct_chat_completions_json_v1"
)
KIMI_NONSTREAMING_PARSER_VERSION: Final = "1.0"
KIMI_API_ORIGIN: Final = "https://api.moonshot.cn"
KIMI_CHAT_PATH: Final = "/v1/chat/completions"
KIMI_MODEL_ID: Final = "kimi-k3"
_MAX_SUCCESS_BODY_BYTES: Final = 512 * 1024
_REQUEST_DEADLINE_SECONDS: Final = 90.0
_CLOSE_TIMEOUT_SECONDS: Final = 5.0
_TOP_LEVEL_REQUIRED = frozenset(
    {"id", "object", "created", "model", "choices", "usage"}
)
_TOP_LEVEL_OPTIONAL = frozenset({"service_tier", "system_fingerprint"})
_CHOICE_REQUIRED = frozenset({"index", "message", "finish_reason"})
_CHOICE_OPTIONAL = frozenset({"logprobs"})
_MESSAGE_REQUIRED = frozenset({"role", "content"})
_MESSAGE_OPTIONAL = frozenset({"reasoning_content", "tool_calls", "refusal"})
_USAGE_REQUIRED = frozenset(
    {"prompt_tokens", "completion_tokens", "total_tokens"}
)
_USAGE_OPTIONAL = frozenset(
    {"cached_tokens", "prompt_tokens_details", "completion_tokens_details"}
)
_PROMPT_TOKEN_DETAIL_FIELDS = frozenset({"audio_tokens", "cached_tokens"})
_COMPLETION_TOKEN_DETAIL_FIELDS = frozenset(
    {
        "accepted_prediction_tokens",
        "audio_tokens",
        "reasoning_tokens",
        "rejected_prediction_tokens",
    }
)
_RESPONSE_SCHEMA_PROFILE: Final = "kimi-k3-json-2026-08-31-v1"


class _NonstreamingJsonInvalid(Exception):
    """Internal control-flow marker that never retains raw JSON."""


@dataclass(frozen=True, slots=True)
class KimiNonstreamingResponse:
    assistant_message: KimiAssistantMessage = field(repr=False)
    finish_reason: str
    usage: KimiChatUsageV3
    latency_ms: int
    http_status: int
    http_attempts: int
    network_calls: int

    def __post_init__(self) -> None:
        if self.finish_reason not in {"stop", "tool_calls"}:
            raise ValueError("Kimi non-streaming finish reason is invalid")
        if (
            self.http_status != 200
            or self.http_attempts != 1
            or self.network_calls != 1
            or self.latency_ms < 0
        ):
            raise ValueError("Kimi non-streaming counters are invalid")

    def usage_dict(self) -> dict[str, int | bool | None]:
        return self.usage.to_dict()


@dataclass(frozen=True, slots=True)
class OfficialSourceCommitment:
    source_id: str
    url: str
    captured_at_utc: str
    decoded_utf8_bytes: int
    sha256: str

    def to_dict(self) -> dict[str, str | int]:
        return {
            "source_id": self.source_id,
            "url": self.url,
            "captured_at_utc": self.captured_at_utc,
            "decoded_utf8_bytes": self.decoded_utf8_bytes,
            "sha256": self.sha256,
        }


OFFICIAL_SOURCE_COMMITMENTS: Final = (
    OfficialSourceCommitment(
        "kimi_k3_quickstart",
        "https://platform.kimi.com/docs/guide/kimi-k3-quickstart.md",
        "2026-08-31T14:03:47.639Z",
        17_475,
        "26d03bce3343f0addd3be22a8cbc6d6cbe7dc00fe635cbe738e574386616af93",
    ),
    OfficialSourceCommitment(
        "kimi_k3_pricing",
        "https://platform.kimi.com/docs/pricing/chat-k3.md",
        "2026-08-31T14:03:47.639Z",
        2_773,
        "e0942aeaf39a8f14697044421a0246036926269a866b63616b3ca4beea014ad9",
    ),
    OfficialSourceCommitment(
        "chat_completions_api",
        "https://platform.kimi.com/docs/api/chat.md",
        "2026-08-31T14:03:47.639Z",
        52_724,
        "e293aab6c8532d8c3fe5fba3ef53b9eb85fd27fe88a20b3eaf7b0d9ad2c903d6",
    ),
    OfficialSourceCommitment(
        "effective_service_terms",
        "https://platform.kimi.com/docs/agreement/modeluse.md",
        "2026-08-31T14:03:47.639Z",
        25_811,
        "8c94cb749ac7a76cde87f97b7c42d20839fda7b128a0c40a28d722730d803ad8",
    ),
    OfficialSourceCommitment(
        "effective_privacy_policy",
        "https://platform.kimi.com/docs/agreement/userprivacy.md",
        "2026-08-31T14:03:47.639Z",
        24_252,
        "3587098df970972804fa3c15aec146e6bded3370dd3c8a653019b05de3cb3b10",
    ),
    OfficialSourceCommitment(
        "effective_payment_terms",
        "https://platform.kimi.com/docs/agreement/payment.md",
        "2026-08-31T14:03:47.639Z",
        6_841,
        "6936e69d09fe7c3823c8fa9ef3f61413cb346f221d73f9508c4b2a00ca566a89",
    ),
)


def _request_payload(request: KimiChatRequest) -> dict[str, Any]:
    if isinstance(request.tool_choice, KimiSpecifiedToolChoice):
        # K3 always reasons.  The Provider documents specified-function choice
        # as incompatible with thinking, so reject it before a Key or network
        # transport can be reached.
        raise KimiChatTransportError("kimi_chat_request_invalid")
    return {
        "model": KIMI_MODEL_ID,
        "messages": [message.to_api_dict() for message in request.messages],
        "tools": [tool.to_api_dict() for tool in request.tools],
        "tool_choice": request.tool_choice,
        "reasoning_effort": request.reasoning_effort,
        "max_completion_tokens": request.max_completion_tokens,
        "stream": False,
    }


def request_body_bytes(request: KimiChatRequest) -> bytes:
    if not isinstance(request, KimiChatRequest):
        raise KimiChatTransportError("kimi_chat_request_invalid")
    body = _v1._canonical_json_bytes(_request_payload(request))
    if len(body) > 6144:
        raise KimiChatTransportError("kimi_chat_request_too_large")
    return body


def _headers(api_key: str) -> dict[str, str]:
    return {
        "authorization": f"Bearer {api_key}",
        "accept": "application/json",
        "accept-encoding": "identity",
        "content-type": "application/json",
        "user-agent": "researchops-kimi-k3-nonstreaming/1.0",
    }


def _exact_or_optional_fields(
    value: Mapping[str, Any], required: frozenset[str], optional: frozenset[str]
) -> bool:
    fields = set(value)
    return required.issubset(fields) and fields.issubset(required | optional)


def _response_error(
    code: str,
    *,
    usage: KimiChatUsageV3 | None = None,
    outcome_unknown: bool = False,
) -> KimiChatTransportError:
    return KimiChatTransportError(
        code,
        http_status=200,
        network_calls=1,
        usage=usage,
        outcome_unknown=outcome_unknown,
    )


def _reject_json_constant(value: str) -> None:
    del value
    raise _NonstreamingJsonInvalid


def _reject_duplicate_keys(
    pairs: list[tuple[str, Any]],
) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _NonstreamingJsonInvalid
        result[key] = value
    return result


def _decode_json_object_without_raw_error(body: bytes) -> dict[str, Any] | None:
    """Decode strict JSON without retaining the input on an exception cause."""

    try:
        value = json.loads(
            body,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_json_constant,
        )
    except (
        UnicodeDecodeError,
        ValueError,
        RecursionError,
        _NonstreamingJsonInvalid,
    ):
        return None
    return value if isinstance(value, dict) else None


def _parse_optional_usage_details(
    value: object,
    *,
    allowed_fields: frozenset[str],
    token_ceiling: int,
) -> dict[str, int | None]:
    if not isinstance(value, dict) or set(value) - allowed_fields:
        raise _response_error("kimi_chat_usage_invalid")
    result: dict[str, int | None] = {}
    for name, raw in value.items():
        if raw is None:
            result[name] = None
            continue
        if type(raw) is not int or raw < 0 or raw > token_ceiling:
            raise _response_error("kimi_chat_usage_invalid")
        result[name] = raw
    return result


def _parse_usage(value: object) -> KimiChatUsageV3:
    if not isinstance(value, dict):
        raise _response_error("kimi_chat_usage_invalid")
    if not _exact_or_optional_fields(value, _USAGE_REQUIRED, _USAGE_OPTIONAL):
        raise _response_error("kimi_chat_usage_invalid")
    core_names = ("prompt_tokens", "completion_tokens", "total_tokens")
    if any(type(value[name]) is not int or value[name] < 0 for name in core_names):
        raise _response_error("kimi_chat_usage_invalid")

    prompt_tokens = value["prompt_tokens"]
    completion_tokens = value["completion_tokens"]
    top_cache_reported = "cached_tokens" in value
    top_cache = value.get("cached_tokens")
    if top_cache_reported and (
        type(top_cache) is not int or top_cache < 0 or top_cache > prompt_tokens
    ):
        raise _response_error("kimi_chat_usage_invalid")

    prompt_details: dict[str, int | None] | None = None
    if "prompt_tokens_details" in value:
        prompt_details = _parse_optional_usage_details(
            value["prompt_tokens_details"],
            allowed_fields=_PROMPT_TOKEN_DETAIL_FIELDS,
            token_ceiling=prompt_tokens,
        )
    if "completion_tokens_details" in value:
        _parse_optional_usage_details(
            value["completion_tokens_details"],
            allowed_fields=_COMPLETION_TOKEN_DETAIL_FIELDS,
            token_ceiling=completion_tokens,
        )

    nested_cache_reported = (
        prompt_details is not None
        and isinstance(prompt_details.get("cached_tokens"), int)
    )
    nested_cache = (
        prompt_details["cached_tokens"] if nested_cache_reported else None
    )
    if top_cache_reported and nested_cache_reported and top_cache != nested_cache:
        raise _response_error("kimi_chat_usage_invalid")
    cached_reported = top_cache_reported or nested_cache_reported
    cached = top_cache if top_cache_reported else nested_cache
    try:
        return KimiChatUsageV3(
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=value["total_tokens"],
            cached_tokens=cached if isinstance(cached, int) else 0,
            cached_tokens_reported=cached_reported,
        )
    except ValueError:
        raise _response_error("kimi_chat_usage_invalid") from None


def _parse_tool_calls(
    value: object,
    *,
    allowed_tool_names: frozenset[str],
    usage: KimiChatUsageV3,
) -> tuple[KimiToolCall, ...]:
    if not isinstance(value, list) or not value or len(value) > 6:
        raise _response_error("kimi_chat_tool_protocol_invalid", usage=usage)
    calls: list[KimiToolCall] = []
    for raw in value:
        if not isinstance(raw, dict) or set(raw) != {"id", "type", "function"}:
            raise _response_error("kimi_chat_tool_protocol_invalid", usage=usage)
        function = raw.get("function")
        if (
            raw.get("type") != "function"
            or not isinstance(function, dict)
            or set(function) != {"name", "arguments"}
            or function.get("name") not in allowed_tool_names
            or not isinstance(function.get("arguments"), str)
        ):
            raise _response_error("kimi_chat_tool_protocol_invalid", usage=usage)
        try:
            calls.append(
                KimiToolCall(
                    call_id=raw["id"],
                    name=function["name"],
                    arguments_json=function["arguments"],
                )
            )
        except (TypeError, ValueError, _v1._JsonInvalid):
            raise _response_error(
                "kimi_chat_tool_protocol_invalid", usage=usage
            ) from None
    if len({call.call_id for call in calls}) != len(calls):
        raise _response_error("kimi_chat_tool_protocol_invalid", usage=usage)
    return tuple(calls)


async def _parse_success(
    response: httpx.Response,
    *,
    latency_started_ns: int,
    allowed_tool_names: frozenset[str],
) -> KimiNonstreamingResponse:
    if (
        _v1._media_type(response) != "application/json"
        or not _v1._content_encoding_is_identity(response)
    ):
        raise _response_error("kimi_chat_response_invalid")
    try:
        body = await _v1._read_bounded_body(response, _MAX_SUCCESS_BODY_BYTES)
    except Exception:
        raise _response_error("kimi_chat_response_invalid") from None
    payload = _decode_json_object_without_raw_error(body)
    body = b""
    if payload is None:
        raise _response_error("kimi_chat_response_invalid") from None

    # Parse a valid usage projection first so every later fail-closed branch can
    # still account for tokens already reported by the Provider.
    if "usage" not in payload:
        raise _response_error("kimi_chat_usage_missing", outcome_unknown=True)
    usage = _parse_usage(payload["usage"])
    if not _exact_or_optional_fields(
        payload, _TOP_LEVEL_REQUIRED, _TOP_LEVEL_OPTIONAL
    ):
        raise _response_error("kimi_chat_response_invalid", usage=usage)
    system_fingerprint = payload.get("system_fingerprint")
    service_tier = payload.get("service_tier")
    if (
        system_fingerprint is not None
        and (
            not isinstance(system_fingerprint, str)
            or not system_fingerprint
            or len(system_fingerprint.encode("utf-8")) > 256
        )
    ) or (
        service_tier is not None
        and (
            not isinstance(service_tier, str)
            or not service_tier
            or len(service_tier.encode("utf-8")) > 64
        )
    ):
        raise _response_error("kimi_chat_response_invalid", usage=usage)
    if (
        not isinstance(payload["id"], str)
        or not payload["id"]
        or payload["object"] != "chat.completion"
        or type(payload["created"]) is not int
        or payload["created"] < 0
        or payload["model"] != KIMI_MODEL_ID
        or not isinstance(payload["choices"], list)
        or len(payload["choices"]) != 1
    ):
        raise _response_error("kimi_chat_response_invalid", usage=usage)
    choice = payload["choices"][0]
    if (
        not isinstance(choice, dict)
        or not _exact_or_optional_fields(choice, _CHOICE_REQUIRED, _CHOICE_OPTIONAL)
        or choice["index"] != 0
        or choice["finish_reason"] not in {"stop", "tool_calls", "length"}
        or choice.get("logprobs") is not None
        or not isinstance(choice["message"], dict)
        or not _exact_or_optional_fields(
            choice["message"], _MESSAGE_REQUIRED, _MESSAGE_OPTIONAL
        )
    ):
        raise _response_error("kimi_chat_response_invalid", usage=usage)
    message = choice["message"]
    content = message.get("content")
    reasoning = message.get("reasoning_content")
    refusal = message.get("refusal")
    if (
        message.get("role") != "assistant"
        or (content is not None and not isinstance(content, str))
        or (reasoning is not None and not isinstance(reasoning, str))
        # ``refusal: null`` is a harmless OpenAI-compatibility projection.  A
        # non-null refusal would carry message state our replay type cannot
        # preserve, so it is rejected rather than silently dropped.
        or refusal is not None
    ):
        raise _response_error("kimi_chat_response_invalid", usage=usage)
    finish_reason = choice["finish_reason"]
    if finish_reason == "length":
        raise _response_error("kimi_chat_output_limit_reached", usage=usage)
    if finish_reason == "tool_calls":
        calls = _parse_tool_calls(
            message.get("tool_calls"),
            allowed_tool_names=allowed_tool_names,
            usage=usage,
        )
    else:
        calls = ()
        raw_tool_calls = message.get("tool_calls")
        if (
            not isinstance(content, str)
            or not content.strip()
            or raw_tool_calls not in (None, [])
        ):
            raise _response_error("kimi_chat_response_invalid", usage=usage)
    try:
        assistant = KimiAssistantMessage(
            content=content,
            reasoning_content=reasoning,
            tool_calls=calls,
        )
    except (TypeError, ValueError):
        raise _response_error("kimi_chat_response_invalid", usage=usage) from None
    return KimiNonstreamingResponse(
        assistant_message=assistant,
        finish_reason=finish_reason,
        usage=usage,
        latency_ms=max(
            0, (time.perf_counter_ns() - latency_started_ns) // 1_000_000
        ),
        http_status=200,
        http_attempts=1,
        network_calls=1,
    )


def _strip_exception_links(error: BaseException) -> BaseException:
    """Remove diagnostic chains that could retain request or response objects."""

    error.__traceback__ = None
    error.__cause__ = None
    error.__context__ = None
    return error


async def run_kimi_nonstreaming_completion(
    request: KimiChatRequest,
    *,
    api_key: str | None = None,
    confirm_online: bool = False,
    _key_loader: Callable[[], str | None] | None = None,
    _transport_factory: Callable[[], httpx.AsyncBaseTransport] | None = None,
) -> KimiNonstreamingResponse:
    if confirm_online is not True:
        api_key = None
        _key_loader = None
        _transport_factory = None
        del request
        raise KimiChatTransportError("kimi_chat_confirmation_required")
    if api_key is not None and _key_loader is not None:
        api_key = None
        _key_loader = None
        _transport_factory = None
        del request
        raise KimiChatTransportError("kimi_chat_configuration_invalid")
    try:
        body = request_body_bytes(request)
    except KimiChatTransportError as exc:
        error = _strip_exception_links(exc)
        api_key = None
        _key_loader = None
        _transport_factory = None
        del request
        raise error from None
    if not _v1._configuration_is_valid():
        body = b""
        api_key = None
        _key_loader = None
        _transport_factory = None
        del request
        raise KimiChatTransportError("kimi_chat_configuration_invalid")
    transport: httpx.AsyncBaseTransport | None = None
    client: httpx.AsyncClient | None = None
    response: httpx.Response | None = None
    result: KimiNonstreamingResponse | None = None
    primary_error: KimiChatTransportError | None = None
    cancelled: asyncio.CancelledError | None = None
    attempts = 0
    started_ns = time.perf_counter_ns()
    try:
        if _key_loader is not None:
            try:
                api_key = _key_loader()
            except Exception:
                raise KimiChatTransportError(
                    "kimi_chat_configuration_invalid"
                ) from None
        if not isinstance(api_key, str) or not api_key or api_key.isspace():
            raise KimiChatTransportError("kimi_chat_key_missing")
        if not _v1._is_safe_api_key(api_key):
            raise KimiChatTransportError("kimi_chat_key_invalid")
        transport = (_transport_factory or _v1._default_transport_factory)()
        client = _v1._build_client(transport)
        attempts = 1
        async with asyncio.timeout(_REQUEST_DEADLINE_SECONDS):
            async with client.stream(
                "POST",
                KIMI_CHAT_PATH,
                headers=_headers(api_key),
                content=body,
            ) as response:
                if response.status_code == 200:
                    try:
                        result = await _parse_success(
                            response,
                            latency_started_ns=started_ns,
                            allowed_tool_names=frozenset(
                                tool.name for tool in request.tools
                            ),
                        )
                    except KimiChatTransportError as exc:
                        if exc.request_id_sha256 is None:
                            exc.request_id_sha256 = _v1._request_id_hash(
                                response, api_key
                            )
                        raise
                else:
                    primary_error = await _v1._classify_http_error(
                        response, api_key=api_key
                    )
    except asyncio.CancelledError as exc:
        cancelled = _strip_exception_links(exc)  # type: ignore[assignment]
    except KimiChatTransportError as exc:
        primary_error = _strip_exception_links(exc)  # type: ignore[assignment]
    except (TimeoutError, httpx.TimeoutException):
        primary_error = KimiChatTransportError(
            "kimi_chat_timeout", network_calls=attempts, outcome_unknown=True
        )
    except httpx.RequestError:
        primary_error = KimiChatTransportError(
            "kimi_chat_network_failed", network_calls=attempts, outcome_unknown=True
        )
    except Exception:
        primary_error = KimiChatTransportError(
            "kimi_chat_failed", network_calls=attempts
        )
    finally:
        close_failed = False
        try:
            if client is not None:
                async with asyncio.timeout(_CLOSE_TIMEOUT_SECONDS):
                    await client.aclose()
            elif transport is not None:
                async with asyncio.timeout(_CLOSE_TIMEOUT_SECONDS):
                    await transport.aclose()
        except asyncio.CancelledError as exc:
            if cancelled is None:
                cancelled = _strip_exception_links(exc)  # type: ignore[assignment]
        except Exception:
            close_failed = True
        finally:
            api_key = None
            body = b""
            response = None
            client = None
            transport = None
            _key_loader = None
            _transport_factory = None
            del request
        if close_failed and primary_error is None and cancelled is None:
            primary_error = KimiChatTransportError(
                "kimi_chat_client_close_failed", network_calls=attempts
            )
        if primary_error is not None or cancelled is not None:
            result = None
    if cancelled is not None:
        raise _strip_exception_links(cancelled) from None
    if primary_error is not None:
        if primary_error.network_calls == 0:
            primary_error.network_calls = attempts
        raise _strip_exception_links(primary_error) from None
    if result is None:
        raise KimiChatTransportError("kimi_chat_failed", network_calls=attempts)
    return result


def kimi_nonstreaming_contract() -> dict[str, Any]:
    return {
        "schema_version": "1.0",
        "contract_id": "kimi-k3-nonstreaming-chat-v1",
        "status": "implemented_offline_tested_not_run",
        "provider_id": "moonshot_kimi",
        "model_id": KIMI_MODEL_ID,
        "transport_id": KIMI_NONSTREAMING_TRANSPORT_ID,
        "api_origin": KIMI_API_ORIGIN,
        "path": KIMI_CHAT_PATH,
        "stream": False,
        "reasoning_effort": "low",
        "first_request_tool_choice": "required",
        "specified_function_tool_choice_allowed": False,
        "complete_assistant_message_replay_required": True,
        "usage_required": True,
        "response_schema_profile": {
            "profile_id": _RESPONSE_SCHEMA_PROFILE,
            "unknown_fields_allowed": False,
            "top_level_optional_compatibility_fields": [
                "service_tier",
                "system_fingerprint",
            ],
            "choice_optional_null_fields": ["logprobs"],
            "message_optional_null_fields": ["refusal"],
            "tool_call_content_may_be_null_empty_or_nonempty_string": True,
        },
        "usage_schema_profile": {
            "required_core_fields": [
                "prompt_tokens",
                "completion_tokens",
                "total_tokens",
            ],
            "accepted_cache_projections": [
                "cached_tokens",
                "prompt_tokens_details.cached_tokens",
                "both_equal",
            ],
            "prompt_tokens_detail_fields": sorted(
                _PROMPT_TOKEN_DETAIL_FIELDS
            ),
            "completion_tokens_detail_fields": sorted(
                _COMPLETION_TOKEN_DETAIL_FIELDS
            ),
            "top_and_nested_cache_must_reconcile": True,
            "unknown_usage_or_detail_fields_allowed": False,
            "missing_usage_error_code": "kimi_chat_usage_missing",
            "missing_usage_outcome_unknown": True,
            "validated_usage_attached_to_later_protocol_errors": True,
        },
        "error_privacy": {
            "raw_json_retained_in_exception_cause": False,
            "key_loader_exception_chained": False,
            "sensitive_locals_cleared_before_terminal_error": True,
            "primary_error_preserved_over_close_error": True,
            "cancellation_propagated_after_owned_close": True,
        },
        "client_retries": 0,
        "fallbacks_allowed": False,
        "synthetic_only": True,
        "raw_prompt_output_reasoning_or_tool_payload_persisted": False,
        "official_sources": [item.to_dict() for item in OFFICIAL_SOURCE_COMMITMENTS],
        "effective_terms_allow_model_service_optimization_use": True,
        "non_synthetic_or_private_allowed": False,
        "network_calls": 0,
        "model_calls": 0,
        "model_quality_claim_allowed": False,
    }


__all__ = [
    "KIMI_NONSTREAMING_PARSER_VERSION",
    "KIMI_NONSTREAMING_TRANSPORT_ID",
    "KimiNonstreamingResponse",
    "OFFICIAL_SOURCE_COMMITMENTS",
    "kimi_nonstreaming_contract",
    "request_body_bytes",
    "run_kimi_nonstreaming_completion",
]
