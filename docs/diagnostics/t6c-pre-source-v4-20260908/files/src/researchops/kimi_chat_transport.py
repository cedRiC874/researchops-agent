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
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Final, TypeAlias

import certifi
import httpx


KIMI_CHAT_PROVIDER_ID: Final = "moonshot_kimi"
KIMI_CHAT_MODEL_ID: Final = "kimi-k3"
KIMI_CHAT_TRANSPORT_ID: Final = "moonshot_direct_chat_completions_sse"
KIMI_CHAT_API_ORIGIN: Final = "https://api.moonshot.cn"
KIMI_CHAT_PATH: Final = "/v1/chat/completions"
KIMI_INVALID_REQUEST_PROBE_BODY: Final = (
    b'{"max_completion_tokens":1,"model":"kimi-k3","reasoning_effort":"low",'
    b'"stream":true,"stream_options":{"include_usage":true}}'
)
KIMI_INVALID_REQUEST_PROBE_BODY_SHA256: Final = (
    "b07a395baa11d449dcb58666363e56daa60a3686edb27bf4e57eb1d8cfa76cd7"
)

_USER_AGENT: Final = "researchops-agent/0.2.0 kimi-chat-transport/1.0"
_EXPECTED_HTTPX_VERSION: Final = "0.28.1"
_EXPECTED_CERTIFI_VERSION: Final = "2026.7.22"
_EXPECTED_HTTPCORE_VERSION: Final = "1.0.9"
_EXPECTED_H11_VERSION: Final = "0.16.0"
_MAX_REQUEST_BODY_BYTES: Final = 6 * 1024
_MAX_ERROR_BODY_BYTES: Final = 64 * 1024
_MAX_SSE_EVENT_BYTES: Final = 64 * 1024
_MAX_SSE_RESPONSE_BYTES: Final = 512 * 1024
_MAX_TOOL_ARGUMENT_BYTES: Final = 4 * 1024
_MAX_TOOL_CALLS_PER_RESPONSE: Final = 6
_MAX_TOOLS_PER_REQUEST: Final = 6
_MAX_COMPLETION_TOKENS: Final = 1536
_CONNECT_TIMEOUT_SECONDS: Final = 5.0
_READ_TIMEOUT_SECONDS: Final = 90.0
_WRITE_TIMEOUT_SECONDS: Final = 5.0
_POOL_TIMEOUT_SECONDS: Final = 5.0
_REQUEST_DEADLINE_SECONDS: Final = 90.0
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

_SAFE_PROVIDER_ID = re.compile(r"^[A-Za-z0-9:_-]{1,256}$")
_SAFE_TOOL_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]{0,255}$")
_SAFE_REQUEST_ID = re.compile(r"^[A-Za-z0-9_-]{1,240}$")
_ALLOWED_PROVIDER_ERROR_TYPES: Final = frozenset(
    {
        "content_filter",
        "invalid_request_error",
        "invalid_authentication_error",
        "incorrect_api_key_error",
        "permission_denied_error",
        "resource_not_found_error",
        "engine_overloaded_error",
        "exceeded_current_quota_error",
        "rate_limit_reached_error",
        "client_closed_request",
        "server_error",
        "unexpected_output",
        "server_unavailable",
    }
)

KIMI_CHAT_STABLE_ERROR_CODES: Final = (
    "kimi_chat_confirmation_required",
    "kimi_chat_configuration_invalid",
    "kimi_chat_key_missing",
    "kimi_chat_key_invalid",
    "kimi_chat_request_invalid",
    "kimi_chat_request_too_large",
    "kimi_chat_redirect_denied",
    "kimi_chat_content_filtered",
    "kimi_chat_invalid_request",
    "kimi_chat_auth_failed",
    "kimi_chat_permission_denied",
    "kimi_chat_model_unavailable",
    "kimi_chat_engine_overloaded",
    "kimi_chat_quota_exceeded",
    "kimi_chat_rate_limited",
    "kimi_chat_client_closed_request",
    "kimi_chat_provider_error",
    "kimi_chat_provider_unavailable",
    "kimi_chat_provider_timeout",
    "kimi_chat_timeout",
    "kimi_chat_network_failed",
    "kimi_chat_response_invalid",
    "kimi_chat_stream_incomplete",
    "kimi_chat_usage_missing",
    "kimi_chat_usage_invalid",
    "kimi_chat_tool_protocol_invalid",
    "kimi_chat_output_limit_reached",
    "kimi_chat_invalid_request_probe_mismatch",
    "kimi_chat_invalid_request_probe_unexpected_status",
    "kimi_chat_client_close_failed",
    "kimi_chat_failed",
)


class _JsonInvalid(Exception):
    pass


class _ResponseTooLarge(Exception):
    pass


class _EventTooLarge(Exception):
    pass


@dataclass(frozen=True, slots=True)
class KimiChatUsage:
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    cached_tokens: int

    def __post_init__(self) -> None:
        values = (
            self.prompt_tokens,
            self.completion_tokens,
            self.total_tokens,
            self.cached_tokens,
        )
        if any(type(value) is not int or value < 0 for value in values):
            raise ValueError("Kimi usage values must be non-negative integers")
        if self.total_tokens != self.prompt_tokens + self.completion_tokens:
            raise ValueError("Kimi total_tokens does not reconcile")
        if self.cached_tokens > self.prompt_tokens:
            raise ValueError("Kimi cached_tokens exceeds prompt_tokens")

    def to_dict(self) -> dict[str, int]:
        return {
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
            "cached_tokens": self.cached_tokens,
        }


@dataclass(frozen=True, slots=True)
class KimiToolCall:
    call_id: str
    name: str
    arguments_json: str = field(repr=False)

    def __post_init__(self) -> None:
        if not _SAFE_PROVIDER_ID.fullmatch(self.call_id):
            raise ValueError("Kimi tool call id is invalid")
        if not _SAFE_TOOL_NAME.fullmatch(self.name):
            raise ValueError("Kimi tool name is invalid")
        if len(self.arguments_json.encode("utf-8")) > _MAX_TOOL_ARGUMENT_BYTES:
            raise ValueError("Kimi tool arguments exceed the transport limit")
        _decode_json_object(self.arguments_json.encode("utf-8"))

    def to_api_dict(self) -> dict[str, Any]:
        return {
            "id": self.call_id,
            "type": "function",
            "function": {
                "name": self.name,
                "arguments": self.arguments_json,
            },
        }

    def __repr__(self) -> str:
        return (
            f"KimiToolCall(call_id={self.call_id!r}, name={self.name!r}, "
            "arguments_json=[REDACTED])"
        )


@dataclass(frozen=True, slots=True)
class KimiTextMessage:
    role: str
    content: str = field(repr=False)

    def __post_init__(self) -> None:
        if self.role not in {"system", "user"}:
            raise ValueError("Kimi text message role is invalid")
        if not isinstance(self.content, str) or not self.content:
            raise ValueError("Kimi text message content is invalid")

    def to_api_dict(self) -> dict[str, str]:
        return {"role": self.role, "content": self.content}

    def __repr__(self) -> str:
        return f"KimiTextMessage(role={self.role!r}, content=[REDACTED])"


@dataclass(frozen=True, slots=True)
class KimiToolResultMessage:
    tool_call_id: str
    name: str
    content: str = field(repr=False)

    def __post_init__(self) -> None:
        if not _SAFE_PROVIDER_ID.fullmatch(self.tool_call_id):
            raise ValueError("Kimi tool result call id is invalid")
        if not _SAFE_TOOL_NAME.fullmatch(self.name):
            raise ValueError("Kimi tool result name is invalid")
        if not isinstance(self.content, str) or not self.content:
            raise ValueError("Kimi tool result content is invalid")

    def to_api_dict(self) -> dict[str, str]:
        return {
            "role": "tool",
            "tool_call_id": self.tool_call_id,
            "name": self.name,
            "content": self.content,
        }

    def __repr__(self) -> str:
        return (
            "KimiToolResultMessage("
            f"tool_call_id={self.tool_call_id!r}, name={self.name!r}, "
            "content=[REDACTED])"
        )


@dataclass(frozen=True, slots=True)
class KimiAssistantMessage:
    content: str | None = field(default=None, repr=False)
    reasoning_content: str | None = field(default=None, repr=False)
    tool_calls: tuple[KimiToolCall, ...] = ()

    def __post_init__(self) -> None:
        if self.content is not None and not isinstance(self.content, str):
            raise ValueError("Kimi assistant content is invalid")
        if self.reasoning_content is not None and not isinstance(
            self.reasoning_content, str
        ):
            raise ValueError("Kimi assistant reasoning is invalid")
        if len(self.tool_calls) > _MAX_TOOL_CALLS_PER_RESPONSE:
            raise ValueError("Kimi assistant tool call count is invalid")
        ids = [call.call_id for call in self.tool_calls]
        if len(ids) != len(set(ids)):
            raise ValueError("Kimi assistant tool call ids are duplicated")

    def to_api_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "role": "assistant",
            "content": self.content,
        }
        if self.reasoning_content is not None:
            result["reasoning_content"] = self.reasoning_content
        if self.tool_calls:
            result["tool_calls"] = [call.to_api_dict() for call in self.tool_calls]
        return result

    def __repr__(self) -> str:
        return (
            "KimiAssistantMessage(content=[REDACTED], "
            "reasoning_content=[REDACTED], "
            f"tool_calls={self.tool_calls!r})"
        )


KimiMessage: TypeAlias = (
    KimiTextMessage | KimiAssistantMessage | KimiToolResultMessage
)


@dataclass(frozen=True, slots=True)
class KimiFunctionTool:
    name: str
    description: str = field(repr=False)
    parameters_json: str = field(repr=False)

    @classmethod
    def from_schema(
        cls, *, name: str, description: str, parameters: Mapping[str, Any]
    ) -> "KimiFunctionTool":
        encoded = _canonical_json_bytes(parameters)
        decoded = _decode_json_object(encoded)
        if decoded.get("type") != "object":
            raise ValueError("Kimi tool schema must have type=object")
        if decoded.get("additionalProperties") is not False:
            raise ValueError("Kimi tool schema must deny additional properties")
        return cls(name, description, encoded.decode("utf-8"))

    def __post_init__(self) -> None:
        if not _SAFE_TOOL_NAME.fullmatch(self.name):
            raise ValueError("Kimi tool name is invalid")
        if not isinstance(self.description, str) or not self.description.strip():
            raise ValueError("Kimi tool description is invalid")
        schema = _decode_json_object(self.parameters_json.encode("utf-8"))
        if schema.get("type") != "object" or schema.get("additionalProperties") is not False:
            raise ValueError("Kimi tool schema is not strict")

    def to_api_dict(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": _decode_json_object(
                    self.parameters_json.encode("utf-8")
                ),
                "strict": True,
            },
        }

    def __repr__(self) -> str:
        return (
            f"KimiFunctionTool(name={self.name!r}, description=[REDACTED], "
            "parameters_json=[REDACTED])"
        )


@dataclass(frozen=True, slots=True)
class KimiSpecifiedToolChoice:
    name: str

    def __post_init__(self) -> None:
        if not _SAFE_TOOL_NAME.fullmatch(self.name):
            raise ValueError("Kimi specified tool name is invalid")

    def to_api_dict(self) -> dict[str, Any]:
        return {"type": "function", "function": {"name": self.name}}


@dataclass(frozen=True, slots=True)
class KimiChatRequest:
    messages: tuple[KimiMessage, ...]
    tools: tuple[KimiFunctionTool, ...] = ()
    tool_choice: str | KimiSpecifiedToolChoice = "none"
    max_completion_tokens: int = _MAX_COMPLETION_TOKENS
    reasoning_effort: str = "low"

    def __post_init__(self) -> None:
        if not self.messages:
            raise ValueError("Kimi chat messages must not be empty")
        if len(self.tools) > _MAX_TOOLS_PER_REQUEST:
            raise ValueError("Kimi tool count exceeds the transport limit")
        names = [tool.name for tool in self.tools]
        if len(names) != len(set(names)):
            raise ValueError("Kimi tool names are duplicated")
        if isinstance(self.tool_choice, str):
            if self.tool_choice not in {"required", "none"}:
                raise ValueError("Kimi tool_choice is not allowed")
            if self.tool_choice == "required" and not self.tools:
                raise ValueError("Kimi required tool_choice needs a tool")
        elif self.tool_choice.name not in names:
            raise ValueError("Kimi specified tool_choice is not declared")
        if (
            type(self.max_completion_tokens) is not int
            or not 1 <= self.max_completion_tokens <= _MAX_COMPLETION_TOKENS
        ):
            raise ValueError("Kimi max_completion_tokens is invalid")
        if self.reasoning_effort != "low":
            raise ValueError("Kimi controlled transport requires reasoning_effort=low")
        _validate_message_sequence(self.messages)
        if len(self.to_body_bytes()) > _MAX_REQUEST_BODY_BYTES:
            raise ValueError("Kimi canonical request exceeds 6 KiB")

    def to_payload(self) -> dict[str, Any]:
        choice: str | dict[str, Any]
        if isinstance(self.tool_choice, KimiSpecifiedToolChoice):
            choice = self.tool_choice.to_api_dict()
        else:
            choice = self.tool_choice
        return {
            "model": KIMI_CHAT_MODEL_ID,
            "messages": [message.to_api_dict() for message in self.messages],
            "tools": [tool.to_api_dict() for tool in self.tools],
            "tool_choice": choice,
            "reasoning_effort": self.reasoning_effort,
            "max_completion_tokens": self.max_completion_tokens,
            "stream": True,
            "stream_options": {"include_usage": True},
        }

    def to_body_bytes(self) -> bytes:
        return _canonical_json_bytes(self.to_payload())

    def body_sha256(self) -> str:
        return hashlib.sha256(self.to_body_bytes()).hexdigest()


@dataclass(frozen=True, slots=True)
class KimiChatResponse:
    assistant_message: KimiAssistantMessage = field(repr=False)
    finish_reason: str
    usage: KimiChatUsage
    latency_ms: int
    http_status: int
    http_attempts: int
    network_calls: int
    completion_id_sha256: str | None
    request_id_sha256: str | None

    def __post_init__(self) -> None:
        if self.finish_reason not in {"stop", "tool_calls"}:
            raise ValueError("Kimi response finish reason is not complete")
        if self.http_status != 200 or self.http_attempts != 1 or self.network_calls != 1:
            raise ValueError("Kimi response transport counters are invalid")


@dataclass(frozen=True, slots=True)
class KimiInvalidRequestProbeResult:
    """Evidence that the fixed promptless request was rejected as documented."""

    latency_ms: int
    http_status: int
    http_attempts: int
    network_calls: int
    provider_error_type: str
    request_id_sha256: str | None = None

    def __post_init__(self) -> None:
        if (
            self.latency_ms < 0
            or self.http_status != 400
            or self.http_attempts != 1
            or self.network_calls != 1
            or self.provider_error_type != "invalid_request_error"
        ):
            raise ValueError("Kimi invalid-request probe result is invalid")


class KimiChatTransportError(RuntimeError):
    """A stable redacted transport failure safe for an orchestrator receipt."""

    def __init__(
        self,
        code: str,
        *,
        http_status: int | None = None,
        network_calls: int = 0,
        request_id_sha256: str | None = None,
        provider_error_type: str | None = None,
        outcome_unknown: bool = False,
        usage: KimiChatUsage | None = None,
    ) -> None:
        if code not in KIMI_CHAT_STABLE_ERROR_CODES:
            code = "kimi_chat_failed"
        super().__init__(code)
        self.code = code
        self.http_status = http_status
        self.network_calls = network_calls
        self.request_id_sha256 = request_id_sha256
        self.provider_error_type = (
            provider_error_type
            if provider_error_type in _ALLOWED_PROVIDER_ERROR_TYPES
            else None
        )
        self.outcome_unknown = outcome_unknown
        self.usage = usage

    def to_receipt_fields(self) -> dict[str, Any]:
        return {
            "error_code": self.code,
            "http_status": self.http_status,
            "network_calls": self.network_calls,
            "request_id_sha256": self.request_id_sha256,
            "provider_error_type": self.provider_error_type,
            "outcome_unknown": self.outcome_unknown,
            "usage": self.usage.to_dict() if self.usage is not None else None,
        }

    def __repr__(self) -> str:
        return f"KimiChatTransportError(code={self.code!r}, [REDACTED])"


@dataclass
class _ToolCallBuilder:
    call_id: str | None = None
    call_type: str | None = None
    name: str | None = None
    argument_parts: list[str] = field(default_factory=list)


def _reject_json_constant(value: str) -> None:
    del value
    raise _JsonInvalid


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _JsonInvalid
        result[key] = value
    return result


def _decode_json(body: bytes) -> Any:
    try:
        return json.loads(
            body,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_json_constant,
        )
    except (UnicodeDecodeError, ValueError, RecursionError, _JsonInvalid) as exc:
        raise _JsonInvalid from exc


def _decode_json_object(body: bytes) -> dict[str, Any]:
    value = _decode_json(body)
    if not isinstance(value, dict):
        raise _JsonInvalid
    return value


def _canonical_json_bytes(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise ValueError("Kimi request value is not canonical JSON") from exc


def _validate_message_sequence(messages: Sequence[KimiMessage]) -> None:
    pending: dict[str, str] = {}
    seen_tool_ids: set[str] = set()
    for message in messages:
        if isinstance(message, KimiAssistantMessage):
            if pending:
                raise ValueError("Kimi assistant message precedes unresolved tools")
            pending = {call.call_id: call.name for call in message.tool_calls}
            if len(pending) != len(message.tool_calls):
                raise ValueError("Kimi assistant tool ids are duplicated")
            if seen_tool_ids.intersection(pending):
                raise ValueError("Kimi tool ids are reused across messages")
            seen_tool_ids.update(pending)
        elif isinstance(message, KimiToolResultMessage):
            expected_name = pending.pop(message.tool_call_id, None)
            if expected_name != message.name:
                raise ValueError("Kimi tool result does not match assistant call")
        elif pending:
            raise ValueError("Kimi tool results are incomplete")
    if pending:
        raise ValueError("Kimi request ends with unresolved tool calls")


def _is_safe_api_key(value: str) -> bool:
    return (
        1 <= len(value) <= 512
        and value == value.strip()
        and all(0x21 <= ord(character) <= 0x7E for character in value)
    )


def _configuration_is_valid() -> bool:
    if (
        KIMI_CHAT_API_ORIGIN != "https://api.moonshot.cn"
        or KIMI_CHAT_PATH != "/v1/chat/completions"
        or len(KIMI_INVALID_REQUEST_PROBE_BODY) != 124
        or hashlib.sha256(KIMI_INVALID_REQUEST_PROBE_BODY).hexdigest()
        != KIMI_INVALID_REQUEST_PROBE_BODY_SHA256
        or _MAX_REQUEST_BODY_BYTES != 6 * 1024
        or _MAX_SSE_EVENT_BYTES != 64 * 1024
        or _MAX_SSE_RESPONSE_BYTES != 512 * 1024
    ):
        return False
    timeout_values = (
        _CONNECT_TIMEOUT_SECONDS,
        _READ_TIMEOUT_SECONDS,
        _WRITE_TIMEOUT_SECONDS,
        _POOL_TIMEOUT_SECONDS,
        _REQUEST_DEADLINE_SECONDS,
        _CLOSE_TIMEOUT_SECONDS,
    )
    if any(
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
        or value <= 0
        for value in timeout_values
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
        http2=False,
    )


def _build_client(transport: httpx.AsyncBaseTransport) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        base_url=KIMI_CHAT_API_ORIGIN,
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
        "accept": "text/event-stream",
        "accept-encoding": "identity",
        "content-type": "application/json",
        "user-agent": _USER_AGENT,
    }


def _request_id_hash(response: httpx.Response, api_key: str) -> str | None:
    value = response.headers.get("msh-request-id")
    return _safe_identifier_hash(value, api_key, pattern=_SAFE_REQUEST_ID)


def _safe_identifier_hash(
    value: str | None, api_key: str, *, pattern: re.Pattern[str] = _SAFE_PROVIDER_ID
) -> str | None:
    if (
        not value
        or not pattern.fullmatch(value)
        or api_key in value
        or value in api_key
    ):
        return None
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _media_type(response: httpx.Response) -> str | None:
    value = response.headers.get("content-type")
    if value is None:
        return None
    return value.split(";", 1)[0].strip().lower()


def _content_encoding_is_identity(response: httpx.Response) -> bool:
    value = response.headers.get("content-encoding")
    return value is None or value.strip().lower() == "identity"


async def _read_bounded_body(response: httpx.Response, limit: int) -> bytes:
    result = bytearray()
    async for chunk in response.aiter_bytes():
        if len(result) + len(chunk) > limit:
            raise _ResponseTooLarge
        result.extend(chunk)
    return bytes(result)


def _parse_usage(value: object) -> KimiChatUsage:
    if not isinstance(value, dict):
        raise KimiChatTransportError("kimi_chat_usage_invalid", network_calls=1)
    expected = {
        "prompt_tokens",
        "completion_tokens",
        "total_tokens",
        "cached_tokens",
    }
    if set(value) != expected:
        raise KimiChatTransportError("kimi_chat_usage_invalid", network_calls=1)
    try:
        return KimiChatUsage(
            prompt_tokens=value["prompt_tokens"],
            completion_tokens=value["completion_tokens"],
            total_tokens=value["total_tokens"],
            cached_tokens=value["cached_tokens"],
        )
    except ValueError as exc:
        raise KimiChatTransportError(
            "kimi_chat_usage_invalid", network_calls=1
        ) from exc


def _apply_tool_fragments(
    builders: dict[int, _ToolCallBuilder], value: object
) -> None:
    if not isinstance(value, list):
        raise KimiChatTransportError(
            "kimi_chat_tool_protocol_invalid", network_calls=1
        )
    for fragment in value:
        if not isinstance(fragment, dict):
            raise KimiChatTransportError(
                "kimi_chat_tool_protocol_invalid", network_calls=1
            )
        index = fragment.get("index")
        if type(index) is not int or not 0 <= index < _MAX_TOOL_CALLS_PER_RESPONSE:
            raise KimiChatTransportError(
                "kimi_chat_tool_protocol_invalid", network_calls=1
            )
        builder = builders.setdefault(index, _ToolCallBuilder())
        call_id = fragment.get("id")
        if call_id is not None:
            if not isinstance(call_id, str) or (
                builder.call_id is not None and builder.call_id != call_id
            ):
                raise KimiChatTransportError(
                    "kimi_chat_tool_protocol_invalid", network_calls=1
                )
            builder.call_id = call_id
        call_type = fragment.get("type")
        if call_type is not None:
            if call_type != "function" or (
                builder.call_type is not None and builder.call_type != call_type
            ):
                raise KimiChatTransportError(
                    "kimi_chat_tool_protocol_invalid", network_calls=1
                )
            builder.call_type = call_type
        function = fragment.get("function")
        if function is not None:
            if not isinstance(function, dict):
                raise KimiChatTransportError(
                    "kimi_chat_tool_protocol_invalid", network_calls=1
                )
            name = function.get("name")
            if name is not None:
                if not isinstance(name, str) or (
                    builder.name is not None and builder.name != name
                ):
                    raise KimiChatTransportError(
                        "kimi_chat_tool_protocol_invalid", network_calls=1
                    )
                builder.name = name
            arguments = function.get("arguments")
            if arguments is not None:
                if not isinstance(arguments, str):
                    raise KimiChatTransportError(
                        "kimi_chat_tool_protocol_invalid", network_calls=1
                    )
                builder.argument_parts.append(arguments)
                if (
                    len("".join(builder.argument_parts).encode("utf-8"))
                    > _MAX_TOOL_ARGUMENT_BYTES
                ):
                    raise KimiChatTransportError(
                        "kimi_chat_tool_protocol_invalid", network_calls=1
                    )


def _finalize_tool_calls(
    builders: dict[int, _ToolCallBuilder], *, api_key: str, allowed_names: frozenset[str]
) -> tuple[KimiToolCall, ...]:
    if not builders:
        return ()
    if sorted(builders) != list(range(len(builders))):
        raise KimiChatTransportError(
            "kimi_chat_tool_protocol_invalid", network_calls=1
        )
    result: list[KimiToolCall] = []
    for index in range(len(builders)):
        builder = builders[index]
        if (
            builder.call_id is None
            or builder.call_type != "function"
            or builder.name is None
            or builder.name not in allowed_names
            or api_key in builder.call_id
            or builder.call_id in api_key
        ):
            raise KimiChatTransportError(
                "kimi_chat_tool_protocol_invalid", network_calls=1
            )
        arguments = "".join(builder.argument_parts)
        try:
            result.append(KimiToolCall(builder.call_id, builder.name, arguments))
        except (ValueError, _JsonInvalid) as exc:
            raise KimiChatTransportError(
                "kimi_chat_tool_protocol_invalid", network_calls=1
            ) from exc
    return tuple(result)


async def _iter_sse_data(response: httpx.Response):
    buffer = bytearray()
    total = 0
    async for chunk in response.aiter_bytes():
        total += len(chunk)
        if total > _MAX_SSE_RESPONSE_BYTES:
            raise _ResponseTooLarge
        buffer.extend(chunk)
        while True:
            lf = buffer.find(b"\n\n")
            crlf = buffer.find(b"\r\n\r\n")
            candidates = [value for value in (lf, crlf) if value >= 0]
            if not candidates:
                if len(buffer) > _MAX_SSE_EVENT_BYTES:
                    raise _EventTooLarge
                break
            boundary = min(candidates)
            separator_length = 4 if crlf == boundary else 2
            event = bytes(buffer[:boundary])
            del buffer[: boundary + separator_length]
            if len(event) > _MAX_SSE_EVENT_BYTES:
                raise _EventTooLarge
            try:
                lines = event.decode("utf-8").splitlines()
            except UnicodeDecodeError as exc:
                raise _JsonInvalid from exc
            data_lines: list[str] = []
            for line in lines:
                if not line or line.startswith(":"):
                    continue
                if not line.startswith("data:"):
                    raise _JsonInvalid
                value = line[5:]
                if value.startswith(" "):
                    value = value[1:]
                data_lines.append(value)
            if data_lines:
                yield "\n".join(data_lines)
    if bytes(buffer).strip():
        raise _JsonInvalid


async def _parse_success_response(
    response: httpx.Response,
    *,
    api_key: str,
    latency_start_ns: int,
    allowed_tool_names: frozenset[str],
) -> KimiChatResponse:
    if _media_type(response) != "text/event-stream" or not _content_encoding_is_identity(
        response
    ):
        raise KimiChatTransportError(
            "kimi_chat_response_invalid",
            http_status=200,
            network_calls=1,
            request_id_sha256=_request_id_hash(response, api_key),
        )
    completion_id: str | None = None
    role: str | None = None
    content_parts: list[str] = []
    reasoning_parts: list[str] = []
    content_started = False
    tool_builders: dict[int, _ToolCallBuilder] = {}
    finish_reason: str | None = None
    usage: KimiChatUsage | None = None
    done_count = 0
    usage_seen = False
    try:
        async for data in _iter_sse_data(response):
            if data == "[DONE]":
                done_count += 1
                if done_count != 1:
                    raise KimiChatTransportError(
                        "kimi_chat_stream_incomplete", network_calls=1
                    )
                continue
            if done_count:
                raise KimiChatTransportError(
                    "kimi_chat_stream_incomplete", network_calls=1
                )
            payload = _decode_json_object(data.encode("utf-8"))
            raw_id = payload.get("id")
            if not isinstance(raw_id, str) or not _SAFE_PROVIDER_ID.fullmatch(raw_id):
                raise KimiChatTransportError(
                    "kimi_chat_response_invalid", network_calls=1
                )
            if completion_id is None:
                completion_id = raw_id
            elif raw_id != completion_id:
                raise KimiChatTransportError(
                    "kimi_chat_response_invalid", network_calls=1
                )
            if payload.get("object") != "chat.completion.chunk":
                raise KimiChatTransportError(
                    "kimi_chat_response_invalid", network_calls=1
                )
            if payload.get("model") != KIMI_CHAT_MODEL_ID:
                raise KimiChatTransportError(
                    "kimi_chat_response_invalid", network_calls=1
                )
            choices = payload.get("choices")
            if not isinstance(choices, list):
                raise KimiChatTransportError(
                    "kimi_chat_response_invalid", network_calls=1
                )
            raw_usage = payload.get("usage")
            if not choices:
                if usage_seen or raw_usage is None or finish_reason is None:
                    raise KimiChatTransportError(
                        "kimi_chat_usage_invalid", network_calls=1
                    )
                usage = _parse_usage(raw_usage)
                usage_seen = True
                continue
            if finish_reason is not None:
                raise KimiChatTransportError(
                    "kimi_chat_response_invalid", network_calls=1
                )
            if usage_seen or len(choices) != 1 or raw_usage not in (None,):
                raise KimiChatTransportError(
                    "kimi_chat_response_invalid", network_calls=1
                )
            choice = choices[0]
            if not isinstance(choice, dict) or choice.get("index") != 0:
                raise KimiChatTransportError(
                    "kimi_chat_response_invalid", network_calls=1
                )
            raw_finish = choice.get("finish_reason")
            if raw_finish is not None:
                if raw_finish not in {"stop", "tool_calls", "length"} or finish_reason is not None:
                    raise KimiChatTransportError(
                        "kimi_chat_response_invalid", network_calls=1
                    )
                finish_reason = raw_finish
            delta = choice.get("delta")
            if not isinstance(delta, dict):
                raise KimiChatTransportError(
                    "kimi_chat_response_invalid", network_calls=1
                )
            raw_role = delta.get("role")
            if raw_role is not None:
                if raw_role != "assistant" or (role is not None and role != raw_role):
                    raise KimiChatTransportError(
                        "kimi_chat_response_invalid", network_calls=1
                    )
                role = raw_role
            reasoning = delta.get("reasoning_content")
            if reasoning is not None:
                if not isinstance(reasoning, str) or content_started:
                    raise KimiChatTransportError(
                        "kimi_chat_response_invalid", network_calls=1
                    )
                reasoning_parts.append(reasoning)
            content = delta.get("content")
            if content is not None:
                if not isinstance(content, str):
                    raise KimiChatTransportError(
                        "kimi_chat_response_invalid", network_calls=1
                    )
                if content:
                    content_started = True
                content_parts.append(content)
            if "tool_calls" in delta:
                _apply_tool_fragments(tool_builders, delta["tool_calls"])
    except KimiChatTransportError:
        raise
    except (_ResponseTooLarge, _EventTooLarge, _JsonInvalid, httpx.DecodingError) as exc:
        raise KimiChatTransportError(
            "kimi_chat_response_invalid", http_status=200, network_calls=1
        ) from exc

    if done_count != 1:
        raise KimiChatTransportError(
            "kimi_chat_stream_incomplete",
            http_status=200,
            network_calls=1,
            outcome_unknown=True,
        )
    if usage is None:
        raise KimiChatTransportError(
            "kimi_chat_usage_missing", http_status=200, network_calls=1
        )
    if completion_id is None or role != "assistant" or finish_reason is None:
        raise KimiChatTransportError(
            "kimi_chat_response_invalid", http_status=200, network_calls=1, usage=usage
        )
    tool_calls = _finalize_tool_calls(
        tool_builders,
        api_key=api_key,
        allowed_names=allowed_tool_names,
    )
    if finish_reason == "tool_calls" and not tool_calls:
        raise KimiChatTransportError(
            "kimi_chat_tool_protocol_invalid", http_status=200, network_calls=1, usage=usage
        )
    if finish_reason != "tool_calls" and tool_calls:
        raise KimiChatTransportError(
            "kimi_chat_tool_protocol_invalid", http_status=200, network_calls=1, usage=usage
        )
    if finish_reason == "length":
        raise KimiChatTransportError(
            "kimi_chat_output_limit_reached", http_status=200, network_calls=1, usage=usage
        )
    if finish_reason == "stop" and not any(part.strip() for part in content_parts):
        raise KimiChatTransportError(
            "kimi_chat_response_invalid", http_status=200, network_calls=1, usage=usage
        )
    assistant = KimiAssistantMessage(
        content="".join(content_parts) if content_parts else None,
        reasoning_content="".join(reasoning_parts) if reasoning_parts else None,
        tool_calls=tool_calls,
    )
    return KimiChatResponse(
        assistant_message=assistant,
        finish_reason=finish_reason,
        usage=usage,
        latency_ms=max(0, (time.perf_counter_ns() - latency_start_ns) // 1_000_000),
        http_status=200,
        http_attempts=1,
        network_calls=1,
        completion_id_sha256=_safe_identifier_hash(completion_id, api_key),
        request_id_sha256=_request_id_hash(response, api_key),
    )


async def _classify_http_error(
    response: httpx.Response, *, api_key: str
) -> KimiChatTransportError:
    status = response.status_code
    request_hash = _request_id_hash(response, api_key)
    if 300 <= status < 400:
        return KimiChatTransportError(
            "kimi_chat_redirect_denied",
            http_status=status,
            network_calls=1,
            request_id_sha256=request_hash,
        )
    if status == 504:
        return KimiChatTransportError(
            "kimi_chat_provider_timeout",
            http_status=status,
            network_calls=1,
            request_id_sha256=request_hash,
            outcome_unknown=True,
        )
    provider_type: str | None = None
    if _media_type(response) == "application/json" and _content_encoding_is_identity(
        response
    ):
        try:
            payload = _decode_json_object(
                await _read_bounded_body(response, _MAX_ERROR_BODY_BYTES)
            )
            error = payload.get("error")
            if isinstance(error, dict) and isinstance(error.get("type"), str):
                candidate = error["type"]
                if candidate in _ALLOWED_PROVIDER_ERROR_TYPES:
                    provider_type = candidate
        except (_JsonInvalid, _ResponseTooLarge, httpx.DecodingError):
            provider_type = None
    if status == 400:
        code = (
            "kimi_chat_content_filtered"
            if provider_type == "content_filter"
            else "kimi_chat_invalid_request"
        )
    elif status == 401:
        code = "kimi_chat_auth_failed"
    elif status == 403:
        code = "kimi_chat_permission_denied"
    elif status == 404:
        code = "kimi_chat_model_unavailable"
    elif status == 429:
        code = {
            "engine_overloaded_error": "kimi_chat_engine_overloaded",
            "exceeded_current_quota_error": "kimi_chat_quota_exceeded",
            "rate_limit_reached_error": "kimi_chat_rate_limited",
        }.get(provider_type, "kimi_chat_rate_limited")
    elif status == 499:
        code = "kimi_chat_client_closed_request"
    elif status == 500:
        code = "kimi_chat_provider_error"
    elif status == 503 or 500 <= status < 600:
        code = "kimi_chat_provider_unavailable"
    else:
        code = "kimi_chat_failed"
    return KimiChatTransportError(
        code,
        http_status=status,
        network_calls=1,
        request_id_sha256=request_hash,
        provider_error_type=provider_type,
        outcome_unknown=status in {499} or status >= 500,
    )


async def run_kimi_chat_completion(
    request: KimiChatRequest,
    *,
    api_key: str | None = None,
    confirm_online: bool = False,
    _key_loader: Callable[[], str | None] | None = None,
    _transport_factory: Callable[[], httpx.AsyncBaseTransport] | None = None,
) -> KimiChatResponse:
    """Perform exactly one owned fixed-origin Kimi Chat Completions attempt.

    The function never reads environment variables, retries, follows redirects,
    falls back, logs bodies, or persists prompts and responses. The underscore
    arguments are offline-test seams and are not authorization boundaries.
    """

    if not isinstance(request, KimiChatRequest):
        raise KimiChatTransportError("kimi_chat_request_invalid")
    if api_key is not None and _key_loader is not None:
        raise KimiChatTransportError("kimi_chat_configuration_invalid")
    if confirm_online is not True:
        raise KimiChatTransportError("kimi_chat_confirmation_required")
    try:
        body = request.to_body_bytes()
    except Exception as exc:
        raise KimiChatTransportError("kimi_chat_request_invalid") from exc
    if len(body) > _MAX_REQUEST_BODY_BYTES:
        raise KimiChatTransportError("kimi_chat_request_too_large")
    if not _configuration_is_valid():
        raise KimiChatTransportError("kimi_chat_configuration_invalid")
    if _key_loader is not None:
        try:
            api_key = _key_loader()
        except Exception as exc:
            raise KimiChatTransportError("kimi_chat_configuration_invalid") from exc
    if not isinstance(api_key, str) or not api_key or api_key.isspace():
        raise KimiChatTransportError("kimi_chat_key_missing")
    if not _is_safe_api_key(api_key):
        raise KimiChatTransportError("kimi_chat_key_invalid")

    transport: httpx.AsyncBaseTransport | None = None
    client: httpx.AsyncClient | None = None
    result: KimiChatResponse | None = None
    primary_error: KimiChatTransportError | None = None
    cancelled: asyncio.CancelledError | None = None
    attempts = 0
    started_ns = time.perf_counter_ns()
    try:
        try:
            transport = (_transport_factory or _default_transport_factory)()
            client = _build_client(transport)
        except asyncio.CancelledError as exc:
            cancelled = exc
        except Exception:
            primary_error = KimiChatTransportError("kimi_chat_failed")
        if client is not None and cancelled is None and primary_error is None:
            attempts = 1
            try:
                async with asyncio.timeout(_REQUEST_DEADLINE_SECONDS):
                    async with client.stream(
                        "POST",
                        KIMI_CHAT_PATH,
                        headers=_request_headers(api_key),
                        content=body,
                    ) as response:
                        if response.status_code == 200:
                            try:
                                result = await _parse_success_response(
                                    response,
                                    api_key=api_key,
                                    latency_start_ns=started_ns,
                                    allowed_tool_names=frozenset(
                                        tool.name for tool in request.tools
                                    ),
                                )
                            except KimiChatTransportError as exc:
                                if exc.http_status is None:
                                    exc.http_status = 200
                                if exc.request_id_sha256 is None:
                                    exc.request_id_sha256 = _request_id_hash(
                                        response, api_key
                                    )
                                raise
                        else:
                            primary_error = await _classify_http_error(
                                response, api_key=api_key
                            )
            except asyncio.CancelledError as exc:
                cancelled = exc
            except KimiChatTransportError as exc:
                if exc.http_status is None:
                    exc.http_status = 200 if result is not None else None
                if exc.network_calls == 0:
                    exc.network_calls = attempts
                primary_error = exc
            except (TimeoutError, httpx.TimeoutException):
                primary_error = KimiChatTransportError(
                    "kimi_chat_timeout", network_calls=1, outcome_unknown=True
                )
            except httpx.DecodingError:
                primary_error = KimiChatTransportError(
                    "kimi_chat_response_invalid",
                    http_status=200,
                    network_calls=1,
                )
            except httpx.RequestError:
                primary_error = KimiChatTransportError(
                    "kimi_chat_network_failed", network_calls=1, outcome_unknown=True
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
                cancelled = exc
        except Exception:
            close_failed = True
        finally:
            api_key = None
        if close_failed and primary_error is None and cancelled is None:
            primary_error = KimiChatTransportError(
                "kimi_chat_client_close_failed", network_calls=attempts
            )

    if cancelled is not None:
        raise cancelled
    if primary_error is not None:
        raise primary_error
    if result is None:
        raise KimiChatTransportError("kimi_chat_failed", network_calls=attempts)
    return result


async def run_kimi_invalid_request_probe(
    *,
    api_key: str | None = None,
    confirm_online: bool = False,
    _key_loader: Callable[[], str | None] | None = None,
    _transport_factory: Callable[[], httpx.AsyncBaseTransport] | None = None,
) -> KimiInvalidRequestProbeResult:
    """Send the one frozen promptless body that intentionally omits ``messages``.

    The request has no prompt or tools and bounds a contract-violating 2xx response
    to one completion token.  Only HTTP 400 with ``invalid_request_error`` is a
    successful probe result.  No response body is read for any other status.
    """

    if api_key is not None and _key_loader is not None:
        raise KimiChatTransportError("kimi_chat_configuration_invalid")
    if confirm_online is not True:
        raise KimiChatTransportError("kimi_chat_confirmation_required")
    if not _configuration_is_valid():
        raise KimiChatTransportError("kimi_chat_configuration_invalid")
    body = KIMI_INVALID_REQUEST_PROBE_BODY
    if (
        len(body) != 124
        or hashlib.sha256(body).hexdigest()
        != KIMI_INVALID_REQUEST_PROBE_BODY_SHA256
    ):
        raise KimiChatTransportError("kimi_chat_configuration_invalid")
    if _key_loader is not None:
        try:
            api_key = _key_loader()
        except Exception as exc:
            raise KimiChatTransportError("kimi_chat_configuration_invalid") from exc
    if not isinstance(api_key, str) or not api_key or api_key.isspace():
        raise KimiChatTransportError("kimi_chat_key_missing")
    if not _is_safe_api_key(api_key):
        raise KimiChatTransportError("kimi_chat_key_invalid")

    transport: httpx.AsyncBaseTransport | None = None
    client: httpx.AsyncClient | None = None
    result: KimiInvalidRequestProbeResult | None = None
    primary_error: KimiChatTransportError | None = None
    cancelled: asyncio.CancelledError | None = None
    attempts = 0
    started_ns = time.perf_counter_ns()
    try:
        try:
            transport = (_transport_factory or _default_transport_factory)()
            client = _build_client(transport)
        except asyncio.CancelledError as exc:
            cancelled = exc
        except Exception:
            primary_error = KimiChatTransportError("kimi_chat_failed")
        if client is not None and cancelled is None and primary_error is None:
            attempts = 1
            try:
                async with asyncio.timeout(_REQUEST_DEADLINE_SECONDS):
                    async with client.stream(
                        "POST",
                        KIMI_CHAT_PATH,
                        headers=_request_headers(api_key),
                        content=body,
                    ) as response:
                        if response.status_code != 400:
                            primary_error = KimiChatTransportError(
                                "kimi_chat_invalid_request_probe_unexpected_status",
                                http_status=response.status_code,
                                network_calls=1,
                                request_id_sha256=_request_id_hash(response, api_key),
                                outcome_unknown=(
                                    200 <= response.status_code < 300
                                    or response.status_code == 499
                                    or response.status_code >= 500
                                ),
                            )
                        else:
                            classified = await _classify_http_error(
                                response, api_key=api_key
                            )
                            if (
                                classified.code != "kimi_chat_invalid_request"
                                or classified.provider_error_type
                                != "invalid_request_error"
                                or classified.outcome_unknown
                            ):
                                primary_error = KimiChatTransportError(
                                    "kimi_chat_invalid_request_probe_mismatch",
                                    http_status=400,
                                    network_calls=1,
                                    request_id_sha256=(
                                        classified.request_id_sha256
                                    ),
                                    provider_error_type=(
                                        classified.provider_error_type
                                    ),
                                )
                            else:
                                result = KimiInvalidRequestProbeResult(
                                    latency_ms=max(
                                        0,
                                        (
                                            time.perf_counter_ns() - started_ns
                                        )
                                        // 1_000_000,
                                    ),
                                    http_status=400,
                                    http_attempts=1,
                                    network_calls=1,
                                    provider_error_type="invalid_request_error",
                                    request_id_sha256=(
                                        classified.request_id_sha256
                                    ),
                                )
            except asyncio.CancelledError as exc:
                cancelled = exc
            except KimiChatTransportError as exc:
                if exc.network_calls == 0:
                    exc.network_calls = attempts
                primary_error = exc
            except (TimeoutError, httpx.TimeoutException):
                primary_error = KimiChatTransportError(
                    "kimi_chat_timeout", network_calls=1, outcome_unknown=True
                )
            except httpx.RequestError:
                primary_error = KimiChatTransportError(
                    "kimi_chat_network_failed", network_calls=1, outcome_unknown=True
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
                cancelled = exc
        except Exception:
            close_failed = True
        finally:
            api_key = None
        if close_failed and primary_error is None and cancelled is None:
            primary_error = KimiChatTransportError(
                "kimi_chat_client_close_failed", network_calls=attempts
            )

    if cancelled is not None:
        raise cancelled
    if primary_error is not None:
        raise primary_error
    if result is None:
        raise KimiChatTransportError("kimi_chat_failed", network_calls=attempts)
    return result
